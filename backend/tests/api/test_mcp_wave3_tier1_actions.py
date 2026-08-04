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
from typing import Any

import pytest_asyncio
from app.core.scopes import Scope
from app.dependencies import get_db, get_redis
from app.mcp import protocol
from app.mcp.standalone import create_mcp_app
from app.models.enums import JobStatus, JobType
from app.models.job import Job
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.services.service_account import ServiceAccountService
from app.workers.kafka_consumer import kill_key_for, latency_key_for
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


class _RedisStub:
    def __init__(self) -> None:
        self._store: dict[str, bytes | str] = {}

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
    """First call finds no key → deleted=False; second call is
    served from the idempotency cache → same payload."""
    ac, _ = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    args = {"key": "cache:nope", "idempotency_key": "empty-k-1"}
    first = _content(await _call(ac, token, "invalidate_cache_key", args))
    second = _content(await _call(ac, token, "invalidate_cache_key", args))
    assert first["deleted"] is False
    assert second == first


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
