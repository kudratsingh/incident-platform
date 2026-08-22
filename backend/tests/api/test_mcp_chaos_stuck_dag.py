"""End-to-end tests for the `create_stuck_dag` chaos hook.

Reuses the CHAOS_ENABLED-true reload trick from
`test_mcp_wave2_chaos_hooks` so the decorator fires against patched
settings before `create_mcp_app` mounts the routes.

What has to be proven (the hook exists because the boot-seeded DAG
auto-completes seconds after boot, so no live probe ever saw a stuck
chain):

  * gating — invisible when CHAOS_ENABLED=false, `chaos:invoke` required
  * the manufactured chain is OBSERVABLE through the existing
    `get_dag_state` read tool (the acceptance criterion)
  * the chain resists the platform's own promoter — a real
    `DependencyResolver` fed the upstream parent's `job.completed`
    promotes nothing
  * the ADR 0008 round-trip — `replay_dlq_by_ids` on the root genuinely
    unsticks the chain, and the resolver then drains it step by step
  * `pause_dag` (the remediation the live scenario grades) is
    observable against the chain via `get_dag_state`
  * idempotent repeat vs. drifted-chain refusal
  * reversibility contract — every row carries the top-level
    `seeded_fixture` marker the reset sweep DELETEs on
"""

from __future__ import annotations

import importlib
import json
import uuid
from typing import Any
from unittest.mock import patch

from app.config import Settings, get_settings
from app.core.scopes import Scope
from app.dependencies import get_db, get_redis
from app.mcp import protocol
from app.mcp.registry import _restore_for_tests, _snapshot_for_tests
from app.models.enums import JobStatus
from app.models.job import Job
from app.models.outbox import OutboxEvent
from app.repositories.audit import AuditRepository
from app.repositories.job import JobRepository
from app.repositories.job_dependency import JobDependencyRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.services.service_account import ServiceAccountService
from app.workers.dependency_resolver import DependencyResolver
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class _RedisStub:
    def __init__(self) -> None:
        self._store: dict[str, bytes | str] = {}
        self._ttls: dict[str, int] = {}

    async def get(self, key: str) -> bytes | str | None:
        return self._store.get(key)

    async def set(
        self, key: str, value: bytes | str, ex: int | None = None
    ) -> bool:
        self._store[key] = value
        if ex is not None:
            self._ttls[key] = ex
        return True

    async def mget(self, keys: list[str]) -> list[bytes | str | None]:
        return [self._store.get(k) for k in keys]

    async def ttl(self, key: str) -> int:
        # redis-py semantics: -2 missing, -1 present without expiry.
        if key not in self._store:
            return -2
        return self._ttls.get(key, -1)

    async def delete(self, *keys: str) -> int:
        removed = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                self._ttls.pop(k, None)
                removed += 1
        return removed


def _mcp_app_with_chaos_enabled(  # type: ignore[no-untyped-def]
    db_session: AsyncSession, redis_stub: _RedisStub
):
    """Fresh MCP app under CHAOS_ENABLED=true. Wipes the registry and
    reloads only the modules these tests invoke, so the surface stays
    minimal and collisions with other files' harnesses are impossible."""
    with patch(
        "app.mcp.standalone.assert_chaos_gate", lambda *a, **kw: None
    ), patch(
        "app.mcp.chaos.get_settings",
        return_value=Settings(chaos_enabled=True, environment="test"),
    ):
        from app.mcp.tools import chaos as chaos_pkg

        snap = _snapshot_for_tests()
        _restore_for_tests({})
        importlib.reload(chaos_pkg.create_stuck_dag)  # type: ignore[attr-defined]

        from app.mcp.tools import dag_state as _dag_state
        from app.mcp.tools.actions import pause_dag as _pause_dag
        from app.mcp.tools.actions import replay_dlq_by_ids as _replay

        for _mod in (_dag_state, _pause_dag, _replay):
            importlib.reload(_mod)

        from app.mcp.standalone import create_mcp_app

        app = create_mcp_app()

    async def _override_db():  # type: ignore[no-untyped-def]
        yield db_session

    async def _override_redis():  # type: ignore[no-untyped-def]
        yield redis_stub

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis

    def _teardown() -> None:
        _restore_for_tests(snap)

    return app, _teardown


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


class _NoopTxn:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _TxnlessSession:
    """Hands the resolver the test's live session but makes `begin()` a
    no-op — the `db_session` fixture already owns the transaction."""

    def __init__(self, inner: AsyncSession) -> None:
        self._inner = inner

    def begin(self) -> _NoopTxn:
        return _NoopTxn()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _SessionHandle:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> _TxnlessSession:
        return _TxnlessSession(self._session)

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _resolver(db_session: AsyncSession, redis_stub: _RedisStub) -> DependencyResolver:
    return DependencyResolver(
        lambda: _SessionHandle(db_session),  # type: ignore[arg-type]
        redis=redis_stub,
    )


async def _submitted_outbox_job_ids(db_session: AsyncSession) -> list[str]:
    settings = get_settings()
    rows = (
        (
            await db_session.execute(
                select(OutboxEvent).where(
                    OutboxEvent.topic == settings.kafka_topic_job_submitted
                )
            )
        )
        .scalars()
        .all()
    )
    return [str(r.payload["job_id"]) for r in rows]


async def _job(db_session: AsyncSession, job_id: str) -> Job:
    row = (
        await db_session.execute(select(Job).where(Job.id == uuid.UUID(job_id)))
    ).scalar_one()
    await db_session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


async def test_create_stuck_dag_not_registered_when_chaos_disabled(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """No CHAOS_ENABLED patch — the default app must not know the tool."""
    from app.mcp.standalone import create_mcp_app

    redis_stub = _RedisStub()
    app = create_mcp_app()

    async def _override_db():  # type: ignore[no-untyped-def]
        yield db_session

    async def _override_redis():  # type: ignore[no-untyped-def]
        yield redis_stub

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        token = await _token(
            db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
        )
        body = await _call(ac, token, "create_stuck_dag", {})
    assert body["error"]["code"] == protocol.MCP_TOOL_NOT_FOUND


async def test_create_stuck_dag_missing_chaos_scope_is_forbidden(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            token = await _token(
                db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
            )
            body = await _call(ac, token, "create_stuck_dag", {})
        assert body["error"]["code"] == protocol.MCP_FORBIDDEN
    finally:
        teardown()


# ---------------------------------------------------------------------------
# The stuck chain is real and observable
# ---------------------------------------------------------------------------


async def test_stuck_chain_is_observable_through_get_dag_state(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The acceptance criterion: the fault must read back through the
    existing read surface, not through anything chaos-only."""
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            chaos = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            made = _content(
                await _call(ac, chaos, "create_stuck_dag", {"chain_name": "obs"})
            )
            assert made["accepted"] and made["created"]
            assert len(made["waiting_job_ids"]) == 2

            reader = await _token(
                db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
            )
            dag = _content(
                await _call(
                    ac, reader, "get_dag_state", {"job_id": made["root_job_id"]}
                )
            )
        by_id = {n["id"]: n for n in dag["nodes"]}
        assert by_id[made["completed_parent_id"]]["status"] == "completed"
        assert by_id[made["root_job_id"]]["status"] == "dead_letter"
        assert by_id[made["root_job_id"]]["retry_count"] == 3
        assert by_id[made["waiting_job_ids"][0]]["status"] == "waiting"
        assert dag["paused"] is False
        edges = {(e["from_id"], e["to_id"]) for e in dag["edges"]}
        assert (made["root_job_id"], made["completed_parent_id"]) in edges
        assert (made["waiting_job_ids"][0], made["root_job_id"]) in edges

        # Reversibility contract: every row carries the top-level marker
        # the reset sweep's DELETE predicate matches.
        for job_id in (
            made["root_job_id"],
            made["completed_parent_id"],
            *made["waiting_job_ids"],
        ):
            row = await _job(db_session, job_id)
            assert row.payload is not None
            assert row.payload["seeded_fixture"] is True
    finally:
        teardown()


async def test_stuck_chain_resists_the_platforms_own_promoter(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """Feed a real DependencyResolver the upstream parent's
    `job.completed` — exactly the redelivery that used to drain the
    boot-seeded DAG — and prove it promotes nothing: the root is
    `dead_letter` (not WAITING, so skipped) and every descendant still
    has an unmet dependency."""
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            chaos = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            made = _content(
                await _call(
                    ac, chaos, "create_stuck_dag", {"chain_name": "resists"}
                )
            )

        resolver = _resolver(db_session, redis_stub)
        await resolver.handle_message(
            topic="job.completed",
            key="k",
            value={
                "event": "job.completed",
                "job_id": made["completed_parent_id"],
            },
        )

        root = await _job(db_session, made["root_job_id"])
        assert root.status == JobStatus.DEAD_LETTER.value
        for step_id in made["waiting_job_ids"]:
            step = await _job(db_session, step_id)
            assert step.status == JobStatus.WAITING.value
        dep_repo = JobDependencyRepository(db_session)
        assert (
            await dep_repo.unmet_count(uuid.UUID(made["waiting_job_ids"][0])) == 1
        )
        assert await _submitted_outbox_job_ids(db_session) == []
    finally:
        teardown()


# ---------------------------------------------------------------------------
# ADR 0008 round-trip: the compensators genuinely work
# ---------------------------------------------------------------------------


async def test_create_stuck_dag_round_trip_with_replay_dlq_by_ids(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The unstick path, end to end through the platform's own
    machinery: replay the dead-lettered root, complete it the way the
    dispatcher would, and watch the real resolver drain the chain."""
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            chaos = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            made = _content(
                await _call(
                    ac, chaos, "create_stuck_dag", {"chain_name": "roundtrip"}
                )
            )

            actions = await _token(
                db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
            )
            replayed = _content(
                await _call(
                    ac,
                    actions,
                    "replay_dlq_by_ids",
                    {
                        "job_ids": [made["root_job_id"]],
                        "idempotency_key": "stuck-dag-roundtrip-1",
                    },
                )
            )
        assert replayed["replayed"] == 1

        root = await _job(db_session, made["root_job_id"])
        assert root.status == JobStatus.PENDING.value
        assert root.retry_count == 0
        assert made["root_job_id"] in await _submitted_outbox_job_ids(db_session)

        # Complete each freed job the way the dispatcher would, then let
        # the real resolver react to its job.completed. The chain drains
        # one step per completion — promotion is real, not simulated.
        job_repo = JobRepository(db_session)
        resolver = _resolver(db_session, redis_stub)
        chain = [made["root_job_id"], *made["waiting_job_ids"]]
        for i, job_id in enumerate(chain):
            await job_repo.update_status(
                uuid.UUID(job_id), JobStatus.COMPLETED
            )
            await resolver.handle_message(
                topic="job.completed",
                key="k",
                value={"event": "job.completed", "job_id": job_id},
            )
            if i + 1 < len(chain):
                nxt = await _job(db_session, chain[i + 1])
                assert nxt.status == JobStatus.PENDING.value

        submitted = await _submitted_outbox_job_ids(db_session)
        for step_id in made["waiting_job_ids"]:
            assert step_id in submitted
        stuck_left = (
            (
                await db_session.execute(
                    select(Job).where(Job.status == JobStatus.WAITING.value)
                )
            )
            .scalars()
            .all()
        )
        assert stuck_left == []
    finally:
        teardown()


async def test_pause_dag_is_observable_on_the_manufactured_chain(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The stabilization the live scenario grades: pause the root, read
    `paused=true` (with expiry) back through `get_dag_state`, and see a
    descendant name the root as the ancestor holding it."""
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            chaos = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            made = _content(
                await _call(
                    ac, chaos, "create_stuck_dag", {"chain_name": "pausable"}
                )
            )

            actions = await _token(
                db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
            )
            paused = _content(
                await _call(
                    ac,
                    actions,
                    "pause_dag",
                    {
                        "root_job_id": made["root_job_id"],
                        "ttl_seconds": 600,
                        "idempotency_key": "stuck-dag-pause-1",
                    },
                )
            )
            assert paused["accepted"] is True

            reader = await _token(
                db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
            )
            dag_root = _content(
                await _call(
                    ac, reader, "get_dag_state", {"job_id": made["root_job_id"]}
                )
            )
            dag_step = _content(
                await _call(
                    ac,
                    reader,
                    "get_dag_state",
                    {"job_id": made["waiting_job_ids"][0]},
                )
            )
        assert dag_root["paused"] is True
        assert dag_root["paused_expires_in_seconds"] == 600
        assert dag_step["paused"] is False
        assert dag_step["paused_by"] == made["root_job_id"]
        by_id = {n["id"]: n for n in dag_step["nodes"]}
        assert by_id[made["waiting_job_ids"][0]]["status"] == "waiting"
    finally:
        teardown()


# ---------------------------------------------------------------------------
# Idempotent repeat vs. drift
# ---------------------------------------------------------------------------


async def test_create_stuck_dag_repeat_is_idempotent_until_drift(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            chaos = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            first = _content(
                await _call(
                    ac, chaos, "create_stuck_dag", {"chain_name": "repeat"}
                )
            )
            second = _content(
                await _call(
                    ac, chaos, "create_stuck_dag", {"chain_name": "repeat"}
                )
            )
            assert first["created"] is True
            assert second["created"] is False
            assert second["root_job_id"] == first["root_job_id"]
            assert second["waiting_job_ids"] == first["waiting_job_ids"]

            # Someone remediates the chain — the hook must refuse to
            # rewrite it rather than quietly re-wedging history.
            await JobRepository(db_session).update_status(
                uuid.UUID(first["root_job_id"]), JobStatus.COMPLETED
            )
            body = await _call(
                ac, chaos, "create_stuck_dag", {"chain_name": "repeat"}
            )
        assert body["error"]["code"] == protocol.MCP_TOOL_ERROR
        assert body["error"]["data"]["error_code"] == "stuck_chain_name_in_use"
    finally:
        teardown()
