"""End-to-end tests for the DLQ categorization surface added in v0.4.0.

Covers:
  - list_dlq_messages exposes remediation_hint and filters by it
  - replay_dlq_by_ids replays targeted set + surfaces per-id errors
  - replay_dlq_by_category walks the safe categories
  - replay_dlq_by_category REFUSES human_required
  - mark_dlq_permanent sets the hint + writes an audit row
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from app.core.scopes import Scope
from app.dependencies import get_db, get_redis
from app.mcp import protocol
from app.mcp.standalone import create_mcp_app
from app.models.audit import AuditLog
from app.models.enums import JobStatus, JobType, RemediationHint
from app.models.job import Job
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.services.service_account import ServiceAccountService
from app.workers import dispatcher, dlq_replay_scheduler
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class _RedisStub:
    """In-memory stand-in that supports the KV + ZSET ops the DLQ
    tools touch (`get/set/delete` for idempotency; `zadd/zrem/eval` for
    scheduled replays). Extend as new op families show up."""

    def __init__(self) -> None:
        self._store: dict[str, bytes | str] = {}
        self._zsets: dict[str, dict[str, float]] = {}

    async def get(self, key: str) -> bytes | str | None:
        return self._store.get(key)

    async def set(
        self, key: str, value: bytes | str, ex: int | None = None
    ) -> bool:
        self._store[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                removed += 1
        return removed

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        zset = self._zsets.setdefault(key, {})
        added = 0
        for member, score in mapping.items():
            if member not in zset:
                added += 1
            zset[member] = score
        return added

    async def zrem(self, key: str, *members: str) -> int:
        zset = self._zsets.get(key, {})
        removed = 0
        for member in members:
            if member in zset:
                del zset[member]
                removed += 1
        return removed

    async def zcard(self, key: str) -> int:
        return len(self._zsets.get(key, {}))

    async def eval(self, script: str, numkeys: int, *args: Any) -> list[str]:
        """Python transcription of `_CLAIM_READY_LUA`.

        The unit tests mock `redis.eval` outright, which is why the
        pre-R2-21 `_ZsetPipe`/`zrangebyscore` stubs went dead the moment
        the drain moved to Lua: nothing in this file could serve an EVAL,
        so nothing covered the drain. Emulating the one script the DLQ
        path uses buys back the behavioural coverage — claim, ack, and
        the expired-claim reclaim that makes a worker crash survivable.

        Scope note, same as `test_queue.py`'s: this is a transcription,
        not the interpreter. The real script's boundedness and reclaim
        markers are pinned by
        `test_claim_lua_reclaims_expired_claims_and_stays_bounded`.
        """
        if script != dlq_replay_scheduler._CLAIM_READY_LUA:
            raise NotImplementedError("stub serves only the claim script")
        assert numkeys == 2
        scheduled_key, inflight_key = str(args[0]), str(args[1])
        now, deadline = float(args[2]), float(args[3])
        scheduled = self._zsets.setdefault(scheduled_key, {})
        inflight = self._zsets.setdefault(inflight_key, {})

        def _due(zset: dict[str, float]) -> list[str]:
            return [
                m
                for m, s in sorted(zset.items(), key=lambda kv: kv[1])
                if s <= now
            ]

        claimed = _due(inflight)[:1000]
        for member in _due(scheduled)[: max(0, 1000 - len(claimed))]:
            del scheduled[member]
            claimed.append(member)
        for member in claimed:
            inflight[member] = deadline
        return claimed


@pytest_asyncio.fixture
async def mcp_client(  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
):
    app = create_mcp_app()
    redis_stub = _RedisStub()

    async def _override_db():
        yield db_session

    async def _override_redis():
        yield redis_stub

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        yield ac


async def _token(
    db_session: AsyncSession, tenant_id: uuid.UUID, scopes: list[str]
) -> str:
    svc = ServiceAccountService(
        ServiceAccountRepository(db_session),
        ServiceAccountTokenRepository(db_session),
        AuditRepository(db_session),
    )
    sa = await svc.create_service_account(
        tenant_id=tenant_id,
        name=f"probe-{uuid.uuid4().hex[:8]}",
        scopes=scopes,
        created_by_user_id=None,
    )
    _, plaintext = await svc.mint_token(
        service_account=sa,
        scopes=None,
        ttl=None,
        minted_by_user_id=None,
    )
    return plaintext


def _rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": params or {}}


async def _call(
    ac: AsyncClient, token: str, tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    resp = await ac.post(
        "/mcp",
        json=_rpc("tools/call", {"name": tool_name, "arguments": arguments}),
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp.json()


def _content(body: dict[str, Any]) -> dict[str, Any]:
    return json.loads(body["result"]["content"][0]["text"])


async def _seed_categorized_dlq(
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    test_user,  # type: ignore[no-untyped-def]
) -> dict[str, uuid.UUID]:
    """One job per category. Returns a hint→job_id map."""
    ids: dict[str, uuid.UUID] = {}
    for hint in (
        RemediationHint.REPLAY_SAFE.value,
        RemediationHint.WAIT_AND_REPLAY.value,
        RemediationHint.HUMAN_REQUIRED.value,
    ):
        job = Job(
            tenant_id=default_tenant.id,
            user_id=test_user.id,
            type=JobType.CSV_UPLOAD.value,
            status=JobStatus.DEAD_LETTER.value,
            error_message=f"synthetic {hint}",
            retry_count=3,
            remediation_hint=hint,
        )
        db_session.add(job)
        await db_session.flush()
        ids[hint] = job.id
    return ids


# ---------------------------------------------------------------------------
# list_dlq_messages — categorization surface
# ---------------------------------------------------------------------------


async def test_list_dlq_exposes_remediation_hint(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    await _seed_categorized_dlq(db_session, default_tenant, test_user)
    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    payload = _content(
        await _call(mcp_client, token, "list_dlq_messages", {})
    )
    hints = {item["remediation_hint"] for item in payload["items"]}
    assert hints == {
        RemediationHint.REPLAY_SAFE.value,
        RemediationHint.WAIT_AND_REPLAY.value,
        RemediationHint.HUMAN_REQUIRED.value,
    }


async def test_list_dlq_filters_by_category(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    await _seed_categorized_dlq(db_session, default_tenant, test_user)
    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    payload = _content(
        await _call(
            mcp_client,
            token,
            "list_dlq_messages",
            {"remediation_hint": RemediationHint.REPLAY_SAFE.value},
        )
    )
    assert payload["total"] == 1
    assert all(
        item["remediation_hint"] == RemediationHint.REPLAY_SAFE.value
        for item in payload["items"]
    )


# ---------------------------------------------------------------------------
# replay_dlq_by_ids
# ---------------------------------------------------------------------------


async def test_replay_dlq_by_ids_replays_targeted_set(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    ids = await _seed_categorized_dlq(db_session, default_tenant, test_user)
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    payload = _content(
        await _call(
            mcp_client,
            token,
            "replay_dlq_by_ids",
            {
                "job_ids": [
                    str(ids[RemediationHint.REPLAY_SAFE.value]),
                    str(ids[RemediationHint.WAIT_AND_REPLAY.value]),
                ],
                "idempotency_key": "replay-ids-0001",
            },
        )
    )
    assert payload["requested"] == 2
    assert payload["replayed"] == 2
    assert payload["failed"] == 0


async def test_replay_dlq_by_ids_reports_per_id_failures(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """A mix of real + missing IDs — the good ones replay, the
    missing one shows up as `ok: false` with the not-found message."""
    ids = await _seed_categorized_dlq(db_session, default_tenant, test_user)
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    bogus = str(uuid.uuid4())
    payload = _content(
        await _call(
            mcp_client,
            token,
            "replay_dlq_by_ids",
            {
                "job_ids": [
                    str(ids[RemediationHint.REPLAY_SAFE.value]),
                    bogus,
                ],
                "idempotency_key": "replay-mixed-0001",
            },
        )
    )
    assert payload["replayed"] == 1
    assert payload["failed"] == 1
    bad = next(r for r in payload["results"] if r["id"] == bogus)
    assert bad["ok"] is False
    assert bad["error"]


# ---------------------------------------------------------------------------
# replay_dlq_by_category
# ---------------------------------------------------------------------------


async def test_replay_dlq_by_category_replays_safe(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    await _seed_categorized_dlq(db_session, default_tenant, test_user)
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    payload = _content(
        await _call(
            mcp_client,
            token,
            "replay_dlq_by_category",
            {
                "category": RemediationHint.REPLAY_SAFE.value,
                "idempotency_key": "cat-safe-0001",
            },
        )
    )
    assert payload["category"] == RemediationHint.REPLAY_SAFE.value
    assert payload["matched"] == 1
    assert payload["replayed"] == 1


async def test_replay_dlq_by_category_refuses_human_required(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The whole point of the category: auto-replay is wrong for
    human_required. Refusal is enforced BEFORE any job is touched."""
    await _seed_categorized_dlq(db_session, default_tenant, test_user)
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    body = await _call(
        mcp_client,
        token,
        "replay_dlq_by_category",
        {
            "category": RemediationHint.HUMAN_REQUIRED.value,
            "idempotency_key": "cat-refused-0001",
        },
    )
    assert body["error"]["code"] == protocol.MCP_TOOL_ERROR
    assert body["error"]["data"]["error_code"] == "dlq_category_refused"


# ---------------------------------------------------------------------------
# mark_dlq_permanent
# ---------------------------------------------------------------------------


async def test_mark_dlq_permanent_sets_hint(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    ids = await _seed_categorized_dlq(db_session, default_tenant, test_user)
    replay_safe_id = ids[RemediationHint.REPLAY_SAFE.value]

    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    payload = _content(
        await _call(
            mcp_client,
            token,
            "mark_dlq_permanent",
            {
                "job_id": str(replay_safe_id),
                "reason": "Analysis shows this hits the same bug every time.",
                "idempotency_key": "mark-0001",
            },
        )
    )
    assert payload["remediation_hint"] == RemediationHint.HUMAN_REQUIRED.value
    assert payload["already_marked"] is False
    assert payload["previous_hint"] == RemediationHint.REPLAY_SAFE.value

    # And an audit row was written
    rows = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "job.marked_permanent")
        )
    ).scalars().all()
    assert list(rows), "expected a job.marked_permanent audit row"
    row = rows[-1]
    assert row.extra_data is not None
    assert "same bug" in row.extra_data["reason"]


async def test_mark_dlq_permanent_idempotent(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    ids = await _seed_categorized_dlq(db_session, default_tenant, test_user)
    already_marked_id = ids[RemediationHint.HUMAN_REQUIRED.value]

    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    payload = _content(
        await _call(
            mcp_client,
            token,
            "mark_dlq_permanent",
            {
                "job_id": str(already_marked_id),
                "reason": "no-op, already marked",
                "idempotency_key": "mark-idem-0001",
            },
        )
    )
    assert payload["already_marked"] is True


async def test_mark_dlq_permanent_refuses_non_dlq(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """A non-DLQ job can't be marked — refuses with not-found shape."""
    job = Job(
        tenant_id=default_tenant.id,
        user_id=test_user.id,
        type=JobType.CSV_UPLOAD.value,
        status=JobStatus.COMPLETED.value,
    )
    db_session.add(job)
    await db_session.flush()

    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    body = await _call(
        mcp_client,
        token,
        "mark_dlq_permanent",
        {
            "job_id": str(job.id),
            "reason": "should refuse — not in DLQ",
            "idempotency_key": "mark-refuse-0001",
        },
    )
    assert body["error"]["code"] == protocol.MCP_TOOL_ERROR
    assert body["error"]["data"]["error_code"] == "not_found"


# ---------------------------------------------------------------------------
# delay_seconds — scheduled DLQ replay (wait_and_replay category)
# ---------------------------------------------------------------------------


async def _redis_stub_from_mcp_client(mcp_client) -> _RedisStub:  # type: ignore[no-untyped-def]
    """Recover the `_RedisStub` the fixture yielded so we can assert
    ZSET side-effects directly. The fixture's dependency override
    generator holds it as its bound `redis_stub` closure — we roundtrip
    through the app to reach it."""
    app = mcp_client._transport.app  # type: ignore[attr-defined]
    override = app.dependency_overrides[get_redis]
    gen = override()
    stub = await gen.__anext__()
    return stub  # type: ignore[no-any-return]


async def test_replay_dlq_by_ids_immediate_behavior_unchanged_without_delay(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """delay_seconds=None (omitted) — same behavior as before v0.4.1."""
    ids = await _seed_categorized_dlq(db_session, default_tenant, test_user)
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    payload = _content(
        await _call(
            mcp_client,
            token,
            "replay_dlq_by_ids",
            {
                "job_ids": [str(ids[RemediationHint.REPLAY_SAFE.value])],
                "idempotency_key": "immediate-no-delay",
            },
        )
    )
    assert payload["replayed"] == 1
    assert payload["scheduled"] == 0
    assert payload["results"][0]["scheduled"] is False
    assert payload["results"][0]["execute_at"] is None


async def test_replay_dlq_by_ids_with_delay_schedules_rather_than_replays(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """delay_seconds=N — pushes onto the ZSET, writes a
    `job.replay_scheduled` audit row, doesn't touch job status."""
    ids = await _seed_categorized_dlq(db_session, default_tenant, test_user)
    job_id = ids[RemediationHint.WAIT_AND_REPLAY.value]
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    payload = _content(
        await _call(
            mcp_client,
            token,
            "replay_dlq_by_ids",
            {
                "job_ids": [str(job_id)],
                "delay_seconds": 60,
                "idempotency_key": "delay-60-0001",
            },
        )
    )
    assert payload["replayed"] == 0
    assert payload["scheduled"] == 1
    result = payload["results"][0]
    assert result["ok"] is True
    assert result["scheduled"] is True
    assert result["execute_at"] is not None

    # ZSET has an entry
    stub = await _redis_stub_from_mcp_client(mcp_client)
    assert (
        len(stub._zsets.get("jobs:dlq_replay_delayed", {})) == 1
    ), "expected one scheduled replay in the ZSET"

    # Job is still in dead_letter — the scheduled path does NOT flip
    # status; only the eventual promote-loop replay does.
    still = (
        await db_session.execute(select(Job).where(Job.id == job_id))
    ).scalar_one()
    assert still.status == JobStatus.DEAD_LETTER.value

    # Audit row was written with the delay + execute_at.
    audit_rows = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "job.replay_scheduled")
        )
    ).scalars().all()
    assert list(audit_rows)
    row = audit_rows[-1]
    assert row.extra_data is not None
    assert row.extra_data["delay_seconds"] == 60
    assert row.extra_data["execute_at"] == result["execute_at"]


async def test_replay_dlq_by_ids_delay_seconds_upper_bound_rejected(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """delay_seconds > 3600 → Pydantic validation error at the RPC
    layer (invalid tool arguments), not a tool error."""
    ids = await _seed_categorized_dlq(db_session, default_tenant, test_user)
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    body = await _call(
        mcp_client,
        token,
        "replay_dlq_by_ids",
        {
            "job_ids": [str(ids[RemediationHint.WAIT_AND_REPLAY.value])],
            "delay_seconds": 7200,
            "idempotency_key": "delay-too-big",
        },
    )
    assert "error" in body
    assert body["error"]["code"] == protocol.JSONRPC_INVALID_PARAMS


async def test_replay_dlq_by_category_with_delay_schedules_all_matched(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """Bulk category replay + delay — every matched job is scheduled,
    none are replayed immediately."""
    # Two wait_and_replay jobs
    for i in range(2):
        db_session.add(
            Job(
                tenant_id=default_tenant.id,
                user_id=test_user.id,
                type=JobType.BULK_API_SYNC.value,
                status=JobStatus.DEAD_LETTER.value,
                error_message=f"transient {i}",
                retry_count=3,
                remediation_hint=RemediationHint.WAIT_AND_REPLAY.value,
            )
        )
    await db_session.flush()

    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    payload = _content(
        await _call(
            mcp_client,
            token,
            "replay_dlq_by_category",
            {
                "category": RemediationHint.WAIT_AND_REPLAY.value,
                "delay_seconds": 120,
                "idempotency_key": "cat-delay-0001",
            },
        )
    )
    assert payload["matched"] == 2
    assert payload["replayed"] == 0
    assert payload["scheduled"] == 2
    assert payload["execute_at"] is not None
    assert len(payload["job_ids"]) == 2

    stub = await _redis_stub_from_mcp_client(mcp_client)
    assert len(stub._zsets.get("jobs:dlq_replay_delayed", {})) == 2


# ---------------------------------------------------------------------------
# R2-21 — scheduled replays are durable and always audited
#
# Three failure shapes, one root cause: the scheduled path was neither
# transactional (audit row written after the zadd, no savepoint, no
# compensation) nor recoverable (the promote loop popped the whole due
# batch destructively before attempting any replay).
# ---------------------------------------------------------------------------


def _armed(stub: _RedisStub) -> set[str]:
    return set(stub._zsets.get(dlq_replay_scheduler.SCHEDULED_KEY, {}))


def _claimed(stub: _RedisStub) -> set[str]:
    return set(stub._zsets.get(dlq_replay_scheduler.INFLIGHT_KEY, {}))


async def _scheduled_audit_resource_ids(db_session: AsyncSession) -> set[str]:
    rows = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "job.replay_scheduled")
        )
    ).scalars().all()
    return {row.resource_id for row in rows if row.resource_id is not None}


class _SessionProxy:
    """Lets the dispatcher's `async with session.begin()` run against the
    test's already-open session by mapping it onto a SAVEPOINT."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def begin(self) -> Any:
        return self._session.begin_nested()

    async def __aenter__(self) -> _SessionProxy:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


def _factory_for(session: AsyncSession) -> Any:
    return lambda: _SessionProxy(session)


async def test_scheduled_branch_survives_a_non_app_error_mid_loop(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The scheduled branch caught only `AppError`, unlike the immediate
    branch directly above it. A non-AppError on the second id aborted the
    whole tool, so the first id stayed armed on the ZSET while its audit
    row died with the request transaction — an agent remediation that
    fires with no audit evidence.

    Post-fix: the loop survives, the first id is armed AND audited, the
    second is neither."""
    ids = await _seed_categorized_dlq(db_session, default_tenant, test_user)
    first = ids[RemediationHint.REPLAY_SAFE.value]
    second = ids[RemediationHint.WAIT_AND_REPLAY.value]
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )

    real_log = AuditRepository.log
    seen = {"n": 0}

    async def _flaky_log(self, action, **kwargs):  # type: ignore[no-untyped-def]
        if action == "job.replay_scheduled":
            seen["n"] += 1
            if seen["n"] == 2:
                raise RuntimeError("audit sink unavailable")
        return await real_log(self, action, **kwargs)

    with patch.object(AuditRepository, "log", _flaky_log):
        body = await _call(
            mcp_client,
            token,
            "replay_dlq_by_ids",
            {
                "job_ids": [str(first), str(second)],
                "delay_seconds": 60,
                "idempotency_key": "sched-midloop-crash",
            },
        )

    assert "error" not in body, body
    payload = _content(body)
    assert payload["scheduled"] == 1
    assert payload["failed"] == 1

    stub = await _redis_stub_from_mcp_client(mcp_client)
    audited = await _scheduled_audit_resource_ids(db_session)
    assert audited == {str(first)}
    assert {m.rsplit(":", 1)[-1] for m in _armed(stub)} == {str(first)}


async def test_scheduled_entry_is_disarmed_when_its_audit_row_is_rolled_back(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """`_schedule_one` wrote the durable ZSET entry BEFORE the audit row
    and never compensated it. Any later failure left a replay that would
    fire with no audit evidence at all.

    Modelled here as a failure that lands after the zadd — the residual
    window once the audit write is moved first (a savepoint release can
    still raise). The savepoint rolls the audit row back, so the entry
    must be zrem'd too: armed-without-audit is the one state that is
    never allowed."""
    ids = await _seed_categorized_dlq(db_session, default_tenant, test_user)
    job_id = ids[RemediationHint.WAIT_AND_REPLAY.value]
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )

    real_arm = dlq_replay_scheduler.arm_replay

    async def _zadd_then_die(redis, **kwargs):  # type: ignore[no-untyped-def]
        await real_arm(redis, **kwargs)
        raise RuntimeError("connection reset after the zadd landed")

    with patch.object(dlq_replay_scheduler, "arm_replay", _zadd_then_die):
        body = await _call(
            mcp_client,
            token,
            "replay_dlq_by_ids",
            {
                "job_ids": [str(job_id)],
                "delay_seconds": 60,
                "idempotency_key": "sched-compensate-0001",
            },
        )

    assert "error" not in body, body
    payload = _content(body)
    assert payload["scheduled"] == 0
    assert payload["failed"] == 1

    stub = await _redis_stub_from_mcp_client(mcp_client)
    assert _armed(stub) == set(), "armed replay survived its rolled-back audit row"
    assert await _scheduled_audit_resource_ids(db_session) == set()


async def test_category_scheduled_branch_matches_the_by_ids_shape(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The two tools' scheduled branches diverged: `by_category` caught
    only AppError and aborted the whole loop. Same seed, same failure
    injection, same expected outcome as the `by_ids` test above."""
    for i in range(2):
        db_session.add(
            Job(
                tenant_id=default_tenant.id,
                user_id=test_user.id,
                type=JobType.BULK_API_SYNC.value,
                status=JobStatus.DEAD_LETTER.value,
                error_message=f"transient {i}",
                retry_count=3,
                remediation_hint=RemediationHint.WAIT_AND_REPLAY.value,
            )
        )
    await db_session.flush()
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )

    real_log = AuditRepository.log
    seen = {"n": 0}

    async def _flaky_log(self, action, **kwargs):  # type: ignore[no-untyped-def]
        if action == "job.replay_scheduled":
            seen["n"] += 1
            if seen["n"] == 2:
                raise RuntimeError("audit sink unavailable")
        return await real_log(self, action, **kwargs)

    with patch.object(AuditRepository, "log", _flaky_log):
        body = await _call(
            mcp_client,
            token,
            "replay_dlq_by_category",
            {
                "category": RemediationHint.WAIT_AND_REPLAY.value,
                "delay_seconds": 120,
                "idempotency_key": "cat-midloop-crash",
            },
        )

    assert "error" not in body, body
    payload = _content(body)
    assert payload["matched"] == 2
    assert payload["scheduled"] == 1
    assert payload["failed"] == 1

    stub = await _redis_stub_from_mcp_client(mcp_client)
    audited = await _scheduled_audit_resource_ids(db_session)
    assert len(audited) == 1
    assert {m.rsplit(":", 1)[-1] for m in _armed(stub)} == audited


async def test_scheduled_replay_drains_through_the_claim_and_is_acked(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """First test in this file to exercise the drain at all. Schedules
    through the real tool, rewinds the score, and runs one promote pass:
    the job flips to PENDING, the canonical `job.replayed` row is written
    and the claim is released."""
    ids = await _seed_categorized_dlq(db_session, default_tenant, test_user)
    job_id = ids[RemediationHint.WAIT_AND_REPLAY.value]
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    await _call(
        mcp_client,
        token,
        "replay_dlq_by_ids",
        {
            "job_ids": [str(job_id)],
            "delay_seconds": 60,
            "idempotency_key": "drain-happy-0001",
        },
    )
    stub = await _redis_stub_from_mcp_client(mcp_client)
    (member,) = _armed(stub)
    stub._zsets[dlq_replay_scheduler.SCHEDULED_KEY][member] = time.time() - 1

    await dispatcher._promote_dlq_replay_once(_factory_for(db_session), stub)

    assert _armed(stub) == set()
    assert _claimed(stub) == set(), "claim was never released"
    replayed = (
        await db_session.execute(select(Job).where(Job.id == job_id))
    ).scalar_one()
    assert replayed.status == JobStatus.PENDING.value
    actions = {
        row.action
        for row in (
            await db_session.execute(
                select(AuditLog).where(AuditLog.job_id == job_id)
            )
        ).scalars().all()
    }
    assert {"job.replay_scheduled", "job.replayed"} <= actions


async def test_scheduled_replay_survives_a_worker_crash_between_claim_and_replay(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """`pop_ready` ZREM'd the whole due batch before any replay was
    attempted, so a crash or redeploy in that window silently discarded
    operator/agent-scheduled replays with no record of the loss.

    Pass 1 dies mid-replay: the entry has left the scheduled set but is
    held as a claim, not lost. Pass 2, after the claim TTL lapses,
    reclaims and fires it."""
    ids = await _seed_categorized_dlq(db_session, default_tenant, test_user)
    job_id = ids[RemediationHint.WAIT_AND_REPLAY.value]
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    await _call(
        mcp_client,
        token,
        "replay_dlq_by_ids",
        {
            "job_ids": [str(job_id)],
            "delay_seconds": 60,
            "idempotency_key": "drain-crash-0001",
        },
    )
    stub = await _redis_stub_from_mcp_client(mcp_client)
    (member,) = _armed(stub)
    stub._zsets[dlq_replay_scheduler.SCHEDULED_KEY][member] = time.time() - 1

    # Pass 1 — worker dies between the claim and the replay.
    with patch(
        "app.services.job.JobService.replay_job",
        new=AsyncMock(side_effect=asyncio.CancelledError()),
    ):
        with pytest.raises(asyncio.CancelledError):
            await dispatcher._promote_dlq_replay_once(
                _factory_for(db_session), stub
            )

    assert _armed(stub) == set()
    assert _claimed(stub) == {member}, "replay was discarded by the crash"
    still = (
        await db_session.execute(select(Job).where(Job.id == job_id))
    ).scalar_one()
    assert still.status == JobStatus.DEAD_LETTER.value

    # The claim TTL lapses (the worker never came back to ack it).
    stub._zsets[dlq_replay_scheduler.INFLIGHT_KEY][member] = time.time() - 1

    # Pass 2 — a healthy worker reclaims and fires it.
    await dispatcher._promote_dlq_replay_once(_factory_for(db_session), stub)

    assert _claimed(stub) == set()
    recovered = (
        await db_session.execute(select(Job).where(Job.id == job_id))
    ).scalar_one()
    assert recovered.status == JobStatus.PENDING.value
