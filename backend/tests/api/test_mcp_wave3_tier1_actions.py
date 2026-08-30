"""End-to-end tests for Wave 3 PR E — 4 Tier 1 idempotent actions +
idempotency dispatch integration.

Covers the memory-called-out PR-E test bar:
  * Double-fire same idempotency key executes once
  * Same key + different args → 409-shaped MCP_TOOL_ERROR
  * Missing idempotency_key → INVALID_PARAMS
Per-tool happy path is one call per file so the section stays legible.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest_asyncio
from app.core.scopes import Scope
from app.dependencies import get_db, get_redis
from app.mcp import protocol
from app.mcp.standalone import create_mcp_app
from app.models.enums import JobStatus, JobType
from app.models.idempotency import IdempotencyRecord
from app.models.job import Job
from app.models.service_account import ServiceAccount
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.services.idempotency import _hash_arguments
from app.services.service_account import ServiceAccountService
from app.workers.kafka_consumer import kill_key_for, latency_key_for
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class _RedisStub:
    def __init__(self) -> None:
        self._store: dict[str, bytes | str] = {}
        # R2-27: a cached replay and a genuine re-execution produce the
        # same payload, so the payload alone cannot tell them apart. The
        # side effect can — count the calls that actually reached Redis.
        self.delete_calls: list[tuple[str, ...]] = []

    async def get(self, key: str) -> bytes | str | None:
        return self._store.get(key)

    async def set(
        self, key: str, value: bytes | str, ex: int | None = None
    ) -> bool:
        self._store[key] = value
        return True

    async def mget(self, keys: list[str]) -> list[bytes | str | None]:
        return [self._store.get(k) for k in keys]

    async def delete(self, *keys: str) -> int:
        self.delete_calls.append(keys)
        removed = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                removed += 1
        return removed


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
        yield ac, redis_stub


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


async def _principal_id(db_session: AsyncSession, token: str) -> uuid.UUID:
    """The service-account id `_token` just minted for. Idempotency
    records are scoped by (tenant, principal, key), so a test that seeds
    one by hand has to seed it under the caller's own principal."""
    prefix = token.split(".", 1)[0]
    sa = (
        await db_session.execute(
            select(ServiceAccount).order_by(ServiceAccount.created_at.desc())
        )
    ).scalars().first()
    assert sa is not None, f"no service account minted (token prefix {prefix})"
    return sa.id


# ---------------------------------------------------------------------------
# Idempotency semantics — the load-bearing tests
# ---------------------------------------------------------------------------


async def test_idempotency_double_fire_executes_once(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """First call executes the tool; second call with the same key +
    same args returns the cached response without re-running."""
    ac, redis_stub = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    # Pre-set the kill key so the first restart_consumer_group actually
    # clears it. The second call should NOT re-execute (i.e. it doesn't
    # need the key to exist any more; the cached response takes over).
    redis_stub._store[kill_key_for("worker-dispatcher")] = "killed"

    args = {
        "consumer_group": "worker-dispatcher",
        "idempotency_key": "restart-1",
    }
    first = _content(await _call(ac, token, "restart_consumer_group", args))
    assert first["kill_key_cleared"] is True

    # Re-set the kill key. If the second call were to actually execute,
    # it would clear it again and report `kill_key_cleared=True`. If
    # the dispatch layer returns the cached response instead, we get
    # back exactly the first call's payload — and the kill key stays
    # in Redis.
    redis_stub._store[kill_key_for("worker-dispatcher")] = "killed"
    second = _content(await _call(ac, token, "restart_consumer_group", args))
    assert second == first
    assert kill_key_for("worker-dispatcher") in redis_stub._store


async def test_idempotency_same_key_different_args_refused(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    ac, _ = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    await _call(
        ac,
        token,
        "restart_consumer_group",
        {"consumer_group": "wd-A", "idempotency_key": "shared-k-1"},
    )
    body = await _call(
        ac,
        token,
        "restart_consumer_group",
        {"consumer_group": "wd-B", "idempotency_key": "shared-k-1"},
    )
    assert body["error"]["code"] == protocol.MCP_TOOL_ERROR
    assert body["error"]["data"]["error_code"] == "idempotency_key_reused"


async def test_missing_idempotency_key_returns_invalid_params(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    ac, _ = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    body = await _call(
        ac,
        token,
        "restart_consumer_group",
        {"consumer_group": "wd"},  # no idempotency_key
    )
    assert body["error"]["code"] == protocol.JSONRPC_INVALID_PARAMS


async def test_wrong_scope_forbidden(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    ac, _ = mcp_client
    # incidents:read is not actions:execute
    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    body = await _call(
        ac,
        token,
        "restart_consumer_group",
        {"consumer_group": "wd", "idempotency_key": "k-1"},
    )
    assert body["error"]["code"] == protocol.MCP_FORBIDDEN


# ---------------------------------------------------------------------------
# Per-tool happy paths
# ---------------------------------------------------------------------------


async def test_restart_consumer_group_clears_kill_key(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    ac, redis_stub = mcp_client
    redis_stub._store[kill_key_for("worker-dispatcher")] = "killed"
    redis_stub._store[latency_key_for("worker-dispatcher")] = "2000"
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    payload = _content(
        await _call(
            ac,
            token,
            "restart_consumer_group",
            {
                "consumer_group": "worker-dispatcher",
                "idempotency_key": "restart-key-1",
            },
        )
    )
    assert payload["kill_key_cleared"] is True
    assert payload["latency_key_cleared"] is True
    assert kill_key_for("worker-dispatcher") not in redis_stub._store
    assert latency_key_for("worker-dispatcher") not in redis_stub._store

    # v0.4.9 leak guard: this tool only requires `actions:execute`, so
    # its response must not name the chaos rig. Spelling out
    # `chaos:kill:*` / `chaos:latency:*` here once sent an agent
    # investigating the harness instead of the fault.
    assert "kill_key" not in payload
    assert "latency_key" not in payload
    assert "chaos" not in json.dumps(payload)


async def test_restart_consumer_group_clears_injected_latency_without_kill(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """inject_latency (no kill) must be remediable by restart alone —
    the doc-code contract the chaos help text promises (`restart clears
    the latency by dropping the consumer's Redis state`)."""
    ac, redis_stub = mcp_client
    redis_stub._store[latency_key_for("worker-dispatcher")] = "2000"
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    payload = _content(
        await _call(
            ac,
            token,
            "restart_consumer_group",
            {
                "consumer_group": "worker-dispatcher",
                "idempotency_key": "restart-latency-key-1",
            },
        )
    )
    assert payload["kill_key_cleared"] is False  # nothing was killed
    assert payload["latency_key_cleared"] is True
    assert latency_key_for("worker-dispatcher") not in redis_stub._store


async def test_replay_dlq_messages_replays_dead_letter_jobs(
    mcp_client, db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    ac, _ = mcp_client
    for _ in range(2):
        db_session.add(
            Job(
                tenant_id=default_tenant.id,
                user_id=test_user.id,
                type=JobType.CSV_UPLOAD.value,
                status=JobStatus.DEAD_LETTER.value,
                retry_count=3,
                error_message="stuck",
            )
        )
    await db_session.flush()

    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    payload = _content(
        await _call(
            ac,
            token,
            "replay_dlq_messages",
            {"idempotency_key": "replay-k-1", "limit": 10},
        )
    )
    assert payload["requested"] == 2
    assert payload["replayed"] == 2
    assert payload["failed"] == 0
    assert len(payload["jobs"]) == 2


async def test_pause_dag_sets_expected_redis_key(
    mcp_client, db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    ac, redis_stub = mcp_client
    root = Job(
        tenant_id=default_tenant.id,
        user_id=test_user.id,
        type=JobType.CSV_UPLOAD.value,
        status=JobStatus.PENDING.value,
    )
    db_session.add(root)
    await db_session.flush()

    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    payload = _content(
        await _call(
            ac,
            token,
            "pause_dag",
            {
                "root_job_id": str(root.id),
                "ttl_seconds": 120,
                "idempotency_key": "pause-k-1",
            },
        )
    )
    assert payload["pause_key"] == f"dag:paused:{root.id}"
    assert redis_stub._store[f"dag:paused:{root.id}"] == "paused"


async def test_pause_dag_unknown_root_not_found(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    ac, _ = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    body = await _call(
        ac,
        token,
        "pause_dag",
        {
            "root_job_id": str(uuid.uuid4()),
            "ttl_seconds": 60,
            "idempotency_key": "pause-nf-1",
        },
    )
    assert body["error"]["code"] == protocol.MCP_TOOL_ERROR
    assert body["error"]["data"]["error_code"] == "not_found"


async def test_invalidate_cache_key_deletes_when_allowed(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    ac, redis_stub = mcp_client
    redis_stub._store["cache:foo"] = "value"
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    payload = _content(
        await _call(
            ac,
            token,
            "invalidate_cache_key",
            {"key": "cache:foo", "idempotency_key": "cache-k-1"},
        )
    )
    assert payload["deleted"] is True
    assert "cache:foo" not in redis_stub._store


async def test_invalidate_cache_key_refuses_disallowed_prefix(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    ac, _ = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    body = await _call(
        ac,
        token,
        "invalidate_cache_key",
        {"key": "users:something", "idempotency_key": "bad-prefix-k-1"},
    )
    assert body["error"]["code"] == protocol.MCP_TOOL_ERROR
    assert body["error"]["data"]["error_code"] == "cache_key_forbidden"


async def test_invalidate_cache_key_idempotent_on_missing_key(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """First call finds no key -> deleted=False; second call is served
    from the idempotency cache.

    The payload assertion alone could not prove that. A cached replay and
    a genuine re-execution both produce `deleted=False` on a missing key,
    so `second == first` held either way and the test would have passed
    against a completely broken cache. Counting the invocations that
    reached Redis is what distinguishes them: exactly one, from the call
    that actually executed."""
    ac, redis_stub = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    args = {"key": "cache:nope", "idempotency_key": "empty-k-1"}
    first = _content(await _call(ac, token, "invalidate_cache_key", args))
    second = _content(await _call(ac, token, "invalidate_cache_key", args))
    assert first["deleted"] is False
    assert second == first
    assert redis_stub.delete_calls == [("cache:nope",)], (
        "the second call re-executed instead of replaying from the cache"
    )


async def test_same_key_different_args_refuses_without_re_executing(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """The other half of the same distinction: reusing a key with
    different arguments must refuse (409-shaped) and must not reach the
    side effect a second time."""
    ac, redis_stub = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    key = "reuse-distinct-k-1"
    await _call(
        ac, token, "invalidate_cache_key", {"key": "cache:a", "idempotency_key": key}
    )
    body = await _call(
        ac, token, "invalidate_cache_key", {"key": "cache:b", "idempotency_key": key}
    )

    assert body["error"]["code"] == protocol.MCP_TOOL_ERROR
    assert body["error"]["data"]["error_code"] == "idempotency_key_reused"
    assert redis_stub.delete_calls == [("cache:a",)], (
        "the refused call still reached Redis"
    )


async def test_expired_but_unreaped_record_is_replaced_not_collided_with(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """R2-27. `lookup` treated an expired record as absent while the
    UNIQUE (tenant, principal, key) index went on holding it, so the
    re-execution's insert collided *after* the action had taken effect.
    #154 stopped that from being a 500; the action still ran uncached,
    which meant the next retry re-ran the side effect too.

    The claim takes the expired record over: the call succeeds, the side
    effect happens exactly once, and the record now holds the fresh
    response rather than the stale one — so a retry replays instead of
    re-executing."""
    ac, redis_stub = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    sa_id = await _principal_id(db_session, token)
    args = {"key": "cache:stale-holder", "idempotency_key": "expired-k-1"}
    db_session.add(
        IdempotencyRecord(
            id=uuid.uuid4(),
            tenant_id=default_tenant.id,
            principal_id=sa_id,
            tool_name="invalidate_cache_key",
            idempotency_key="expired-k-1",
            arguments_hash=_hash_arguments(args),
            response_json={"deleted": True, "key": "cache:stale-holder"},
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    await db_session.flush()

    body = await _call(ac, token, "invalidate_cache_key", args)

    assert "error" not in body, body
    assert len(redis_stub.delete_calls) == 1

    record = (
        await db_session.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.idempotency_key == "expired-k-1"
            )
        )
    ).scalar_one()
    assert record.expires_at is not None
    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    assert expires_at > datetime.now(UTC), (
        "the expired record still squats on the unique key"
    )

    # A retry now replays rather than re-executing.
    await _call(ac, token, "invalidate_cache_key", args)
    assert len(redis_stub.delete_calls) == 1


# ---------------------------------------------------------------------------
# Commit-before-response — SAVEPOINT-per-item contract (#5)
# ---------------------------------------------------------------------------


async def test_replay_dlq_messages_mid_loop_crash_isolates_via_savepoint(
    mcp_client, db_session: AsyncSession, default_tenant, test_user, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    """Contract lock for #5: a non-AppError raised on job N of a batch
    must roll back only that item's writes (savepoint), keep the batch
    going, and return a success shape with `failed=N` — not surface as
    the tool's `except Exception` handler committing a partial replay
    behind an 'internal tool error' response."""
    # Three DLQ jobs. Middle one will trigger the injected crash; the
    # other two should complete via savepoint-committed replays.
    jobs = []
    for i in range(3):
        job = Job(
            tenant_id=default_tenant.id,
            user_id=test_user.id,
            type=JobType.CSV_UPLOAD.value,
            status=JobStatus.DEAD_LETTER.value,
            retry_count=3,
            error_message=f"stuck-{i}",
        )
        db_session.add(job)
        jobs.append(job)
    await db_session.flush()
    doomed_id = jobs[1].id

    # Wrap the real replay_job so the second job raises a non-AppError
    # (SQLAlchemy-flavored to mirror a plausible constraint violation).
    from app.services.job import JobService

    real_replay = JobService.replay_job

    async def _boom_on_middle(
        self, job_id, tenant_id, **kwargs  # type: ignore[no-untyped-def]
    ):
        if job_id == doomed_id:
            raise RuntimeError("simulated mid-loop constraint violation")
        return await real_replay(self, job_id, tenant_id, **kwargs)

    monkeypatch.setattr(JobService, "replay_job", _boom_on_middle)

    ac, _ = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    body = await _call(
        ac,
        token,
        "replay_dlq_messages",
        {"idempotency_key": "midloop-crash-k-1", "limit": 10},
    )
    # Success shape, not internal_error — the savepoint contained the
    # per-item crash and let the batch complete.
    assert "error" not in body, body
    payload = _content(body)
    assert payload["requested"] == 3
    assert payload["replayed"] == 2
    assert payload["failed"] == 1
    replayed_ids = {j["id"] for j in payload["jobs"]}
    assert str(doomed_id) not in replayed_ids

    # And the DB state matches the response: doomed job still DEAD_LETTER,
    # other two flipped to PENDING (savepoint-committed).
    for job in jobs:
        await db_session.refresh(job)
    doomed = next(j for j in jobs if j.id == doomed_id)
    assert doomed.status == JobStatus.DEAD_LETTER.value
    assert doomed.retry_count == 3  # unchanged
    for j in jobs:
        if j.id != doomed_id:
            assert j.status == JobStatus.PENDING.value
            assert j.retry_count == 0


async def test_replay_dlq_by_ids_refuses_a_job_in_a_paused_dag(
    mcp_client, db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """E1-08 through the agent's own surface: `pause_dag` held promotion
    but every replay tool fired straight into the paused DAG.

    The refusal rides the existing per-item savepoint (a JobError is an
    AppError, so it is counted as a failed item) — the batch response
    shape is unchanged, which is what keeps the tool contract frozen.
    """
    ac, redis_stub = mcp_client
    jobs = []
    for i in range(2):
        job = Job(
            tenant_id=default_tenant.id,
            user_id=test_user.id,
            type=JobType.CSV_UPLOAD.value,
            status=JobStatus.DEAD_LETTER.value,
            retry_count=3,
            error_message=f"stuck-{i}",
        )
        db_session.add(job)
        jobs.append(job)
    await db_session.flush()
    paused_id = jobs[1].id
    redis_stub._store[f"dag:paused:{paused_id}"] = "paused"

    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    body = await _call(
        ac,
        token,
        "replay_dlq_by_ids",
        {
            "job_ids": [str(j.id) for j in jobs],
            "idempotency_key": "paused-dag-replay-k-1",
        },
    )
    assert "error" not in body, body
    payload = _content(body)
    assert payload["requested"] == 2
    assert payload["replayed"] == 1
    assert payload["failed"] == 1
    failures = [r for r in payload["results"] if not r["ok"]]
    assert [r["id"] for r in failures] == [str(paused_id)]
    assert "paused" in failures[0]["error"]

    # DB state matches the response: the paused job was not dispatched.
    for job in jobs:
        await db_session.refresh(job)
    assert jobs[1].status == JobStatus.DEAD_LETTER.value
    assert jobs[1].retry_count == 3
    assert jobs[0].status == JobStatus.PENDING.value


async def test_restart_consumer_group_reports_whether_it_knows_the_group(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """R2-17: the tool never checked `consumer_group` against anything,
    so a typo'd name cleared no flags and still answered
    `accepted: true` — the stalled consumer stayed dead while the agent
    read success. No hard whitelist (that would refuse a legitimate
    future group); instead the response says whether the platform
    recognises the name, so a caller can tell a no-op apart from a
    restart."""
    ac, redis_stub = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )

    typo = _content(
        await _call(
            ac,
            token,
            "restart_consumer_group",
            {
                "consumer_group": "worker-dispatchr",
                "idempotency_key": "restart-typo-1",
            },
        )
    )
    # Still accepted — the tool stays permissive by design.
    assert typo["accepted"] is True
    assert typo["group_recognized"] is False
    assert typo["kill_key_cleared"] is False
    assert typo["latency_key_cleared"] is False

    # A real group the platform runs, from settings.
    redis_stub._store[kill_key_for("audit-writer")] = "killed"
    real = _content(
        await _call(
            ac,
            token,
            "restart_consumer_group",
            {
                "consumer_group": "audit-writer",
                "idempotency_key": "restart-real-1",
            },
        )
    )
    assert real["group_recognized"] is True
    assert real["kill_key_cleared"] is True

    # A seeded eval group counts as recognised too — scenarios drive
    # restarts against these names.
    seeded = _content(
        await _call(
            ac,
            token,
            "restart_consumer_group",
            {
                "consumer_group": "billing-consumer",
                "idempotency_key": "restart-seeded-1",
            },
        )
    )
    assert seeded["group_recognized"] is True

    # Leak guard still holds: recognition must not name the chaos rig.
    assert "chaos" not in json.dumps(typo)


async def test_restart_consumer_group_description_does_not_promise_a_restart(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """Contract test: `accepted` means the flags were cleared, not that
    a consumer came back. The description must say what the platform
    actually checks, since `group_recognized: false` is the only signal
    distinguishing a typo from a genuine no-op restart."""
    ac, _ = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    listing = await ac.post(
        "/mcp",
        json=_rpc("tools/list"),
        headers={"Authorization": f"Bearer {token}"},
    )
    tools = {t["name"]: t for t in listing.json()["result"]["tools"]}
    description = tools["restart_consumer_group"]["description"]

    assert "group_recognized" in description, (
        "the field an agent needs to detect a typo'd group is not "
        "documented where the agent reads"
    )
    assert "chaos" not in description.lower()
