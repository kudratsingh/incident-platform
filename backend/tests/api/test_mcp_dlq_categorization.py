"""End-to-end tests for the DLQ categorization surface added in v0.4.0.

Covers:
  - list_dlq_messages exposes remediation_hint and filters by it
  - replay_dlq_by_ids replays targeted set + surfaces per-id errors
  - replay_dlq_by_category walks the safe categories
  - replay_dlq_by_category REFUSES human_required
  - mark_dlq_permanent sets the hint + writes an audit row
"""

from __future__ import annotations

import json
import uuid
from typing import Any

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
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class _RedisStub:
    """In-memory stand-in that supports the KV + ZSET ops the DLQ
    tools touch (`get/set/delete` for idempotency; `zadd/zrangebyscore`
    for scheduled replays). Extend as new op families show up."""

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

    async def zrangebyscore(
        self,
        key: str,
        min_score: float | str,
        max_score: float | str,
        withscores: bool = False,
    ) -> list[Any]:
        zset = self._zsets.get(key, {})
        lo = float("-inf") if min_score == "-inf" else float(min_score)
        hi = float("inf") if max_score == "inf" else float(max_score)
        picked = sorted(
            [(m, s) for m, s in zset.items() if lo <= s <= hi],
            key=lambda x: x[1],
        )
        return picked if withscores else [m for m, _ in picked]

    def pipeline(self) -> _ZsetPipe:
        return _ZsetPipe(self._zsets)

    async def zcard(self, key: str) -> int:
        return len(self._zsets.get(key, {}))


class _ZsetPipe:
    def __init__(self, zsets: dict[str, dict[str, float]]) -> None:
        self._zsets = zsets
        self._ops: list[tuple[str, str, str]] = []  # (op, key, member)

    def zrem(self, key: str, member: str) -> _ZsetPipe:
        self._ops.append(("zrem", key, member))
        return self

    async def execute(self) -> list[int]:
        results: list[int] = []
        for op, key, member in self._ops:
            zset = self._zsets.get(key, {})
            if op == "zrem" and member in zset:
                del zset[member]
                results.append(1)
            else:
                results.append(0)
        return results


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
