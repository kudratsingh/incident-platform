"""End-to-end tests for Wave 2 PR D — the 4 remaining chaos hooks.

Reuses the CHAOS_ENABLED-true reload trick from `test_mcp_wave1_pr_b`
so decorators fire against a patched settings before create_mcp_app
mounts the routes.

Coverage per tool:
  - Invisible when CHAOS_ENABLED=false (registry omits it)
  - Wrong scope → MCP_FORBIDDEN
  - Happy path: observable side-effect (Redis key, alert row, kafka
    producer called)
"""

from __future__ import annotations

import importlib
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.core.scopes import Scope
from app.dependencies import get_db, get_redis
from app.mcp import protocol
from app.mcp.registry import _restore_for_tests, _snapshot_for_tests
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.services.service_account import ServiceAccountService
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
        # Match redis-py semantics — returns the count of keys that
        # actually existed and were removed. Needed by
        # invalidate_cache_key (round-trip compensator for
        # create_stale_cache).
        removed = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                removed += 1
        return removed

    def pipeline(self) -> _RedisPipeline:
        return _RedisPipeline(self)


class _RedisPipeline:
    def __init__(self, redis: _RedisStub) -> None:
        self.redis = redis
        self.ops: list[tuple[str, bytes | str, int | None]] = []

    def set(self, key: str, value: bytes | str, ex: int | None = None) -> None:
        self.ops.append((key, value, ex))

    async def execute(self) -> list[bool]:
        for key, value, _ex in self.ops:
            self.redis._store[key] = value
        return [True] * len(self.ops)


def _mcp_app_with_chaos_enabled(db_session: AsyncSession, redis_stub: _RedisStub):
    """Build a fresh MCP app under CHAOS_ENABLED=true so chaos tools
    register. Reloads every chaos tool module so their decorators re-
    evaluate against the patched settings."""
    with patch(
        "app.mcp.standalone.assert_chaos_gate", lambda *a, **kw: None
    ), patch(
        "app.mcp.chaos.get_settings",
        return_value=Settings(chaos_enabled=True, environment="test"),
    ):
        from app.mcp.tools import chaos as chaos_pkg

        snap = _snapshot_for_tests()
        _restore_for_tests({})
        importlib.reload(chaos_pkg.kill_consumer)  # type: ignore[attr-defined]
        importlib.reload(chaos_pkg.poison_message)  # type: ignore[attr-defined]
        importlib.reload(chaos_pkg.saturate_redis)  # type: ignore[attr-defined]
        importlib.reload(chaos_pkg.inject_latency)  # type: ignore[attr-defined]
        importlib.reload(chaos_pkg.bad_deploy)  # type: ignore[attr-defined]
        importlib.reload(chaos_pkg.create_bad_data_job)  # type: ignore[attr-defined]
        importlib.reload(chaos_pkg.create_stale_cache)  # type: ignore[attr-defined]
        importlib.reload(chaos_pkg.seed_dlq_messages)  # type: ignore[attr-defined]
        from app.mcp.tools import consumer_lag as _cl
        from app.mcp.tools import list_active_alerts as _laa

        importlib.reload(_cl)
        importlib.reload(_laa)

        # Action tools aren't chaos-gated but the snapshot wipe above
        # cleared them. Re-import so round-trip tests (create_stale_cache
        # → invalidate_cache_key) can invoke the compensator.
        from app.mcp.tools import actions as _actions_pkg

        for _mod in (
            _actions_pkg.invalidate_cache_key,  # type: ignore[attr-defined]
            _actions_pkg.restart_consumer_group,  # type: ignore[attr-defined]
            _actions_pkg.pause_dag,  # type: ignore[attr-defined]
            _actions_pkg.mark_dlq_permanent,  # type: ignore[attr-defined]
            _actions_pkg.replay_dlq_messages,  # type: ignore[attr-defined]
            _actions_pkg.replay_dlq_by_ids,  # type: ignore[attr-defined]
            _actions_pkg.replay_dlq_by_category,  # type: ignore[attr-defined]
        ):
            importlib.reload(_mod)

        from app.mcp.standalone import create_mcp_app

        app = create_mcp_app()

    async def _override_db():
        yield db_session

    async def _override_redis():
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


# ---------------------------------------------------------------------------
# saturate_redis
# ---------------------------------------------------------------------------


async def test_saturate_redis_not_registered_when_chaos_disabled(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """No CHAOS_ENABLED patch — this uses the default mcp app."""
    from app.mcp.standalone import create_mcp_app

    redis_stub = _RedisStub()
    app = create_mcp_app()

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
        token = await _token(
            db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
        )
        body = await _call(
            ac,
            token,
            "saturate_redis",
            {"num_keys": 10, "value_bytes": 32},
        )
    assert body["error"]["code"] == protocol.MCP_TOOL_NOT_FOUND


async def test_saturate_redis_writes_expected_keys(
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
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            payload = _content(
                await _call(
                    ac,
                    token,
                    "saturate_redis",
                    {"num_keys": 25, "value_bytes": 100, "ttl_seconds": 30},
                )
            )
        assert payload["keys_written"] == 25
        assert payload["total_value_bytes"] == 25 * 100
        # Every key should have landed in the stub, under the run_id prefix.
        prefix = payload["key_prefix"]
        assert sum(1 for k in redis_stub._store if k.startswith(prefix)) == 25
    finally:
        teardown()


# ---------------------------------------------------------------------------
# inject_latency
# ---------------------------------------------------------------------------


async def test_inject_latency_sets_expected_redis_key(
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
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            payload = _content(
                await _call(
                    ac,
                    token,
                    "inject_latency",
                    {
                        "consumer_group": "worker-dispatcher",
                        "latency_ms": 500,
                        "ttl_seconds": 60,
                    },
                )
            )
        assert payload["latency_key"] == "chaos:latency:worker-dispatcher"
        assert redis_stub._store["chaos:latency:worker-dispatcher"] == "500"
    finally:
        teardown()


async def test_inject_latency_rejects_out_of_range(
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
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            body = await _call(
                ac,
                token,
                "inject_latency",
                {
                    "consumer_group": "worker-dispatcher",
                    "latency_ms": 999_999,
                },
            )
        assert body["error"]["code"] == protocol.JSONRPC_INVALID_PARAMS
    finally:
        teardown()


# ---------------------------------------------------------------------------
# bad_deploy
# ---------------------------------------------------------------------------


async def test_bad_deploy_fires_alert_and_sets_flag(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        with patch(
            "app.services.alerts.get_settings",
            return_value=Settings(
                alert_webhook_url=None, alert_webhook_secret=None
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
            ) as ac:
                token = await _token(
                    db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
                )
                payload = _content(
                    await _call(
                        ac,
                        token,
                        "bad_deploy",
                        {"label": "v0.4.0-broken", "ttl_seconds": 60},
                    )
                )
        assert payload["label"] == "v0.4.0-broken"
        assert redis_stub._store["chaos:bad_deploy"] == "v0.4.0-broken"

        from app.models.alert import Alert
        from sqlalchemy import select as _select

        rows = (
            await db_session.execute(
                _select(Alert).where(Alert.source == "chaos:bad_deploy")
            )
        ).scalars().all()
        assert list(rows), "expected an alert row"
        assert rows[-1].severity == "critical"
    finally:
        teardown()


# ---------------------------------------------------------------------------
# poison_message
# ---------------------------------------------------------------------------


async def test_poison_message_invokes_kafka_producer(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """Full round-trip is unavailable in unit tests (no broker); assert
    the producer is started + `send_and_wait` is called with the exact
    body bytes we intended."""
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)

    producer = AsyncMock()
    producer.start = AsyncMock()
    producer.stop = AsyncMock()
    producer.send_and_wait = AsyncMock()

    try:
        with patch(
            "aiokafka.AIOKafkaProducer", return_value=producer
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
            ) as ac:
                token = await _token(
                    db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
                )
                payload = _content(
                    await _call(
                        ac,
                        token,
                        "poison_message",
                        {
                            "topic": "job.submitted",
                            "payload": {"totally": "invalid"},
                        },
                    )
                )
        assert payload["topic"] == "job.submitted"
        assert payload["accepted"] is True
        producer.start.assert_awaited()
        producer.send_and_wait.assert_awaited_once()
        args, kwargs = producer.send_and_wait.call_args
        assert args[0] == "job.submitted"
        assert kwargs["value"] == b'{"totally": "invalid"}'
        producer.stop.assert_awaited()
    finally:
        teardown()


async def test_poison_message_also_writes_replay_safe_dlq_entry(
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    test_user,  # type: ignore[no-untyped-def]
) -> None:
    """The synthetic DLQ row is the observable side of the hook — real
    consumers log-and-drop schema errors, so without this row the
    agent's remediation loop has nothing to react to."""
    from app.models.enums import RemediationHint
    from app.models.job import Job
    from sqlalchemy import select as _select

    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)

    producer = AsyncMock()
    producer.start = AsyncMock()
    producer.stop = AsyncMock()
    producer.send_and_wait = AsyncMock()

    try:
        with patch("aiokafka.AIOKafkaProducer", return_value=producer):
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
            ) as ac:
                token = await _token(
                    db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
                )
                payload = _content(
                    await _call(
                        ac,
                        token,
                        "poison_message",
                        {"topic": "job.submitted", "payload": {}},
                    )
                )
        assert payload["accepted"] is True
        assert payload["dlq_job_id"] is not None
        row = (
            await db_session.execute(
                _select(Job).where(
                    Job.id == uuid.UUID(payload["dlq_job_id"])
                )
            )
        ).scalar_one()
        assert row.status == "dead_letter"
        assert row.remediation_hint == RemediationHint.REPLAY_SAFE.value
        assert row.tenant_id == default_tenant.id
    finally:
        teardown()


async def test_poison_message_kafka_unreachable_returns_clean_error(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """If Kafka is unreachable the tool must return a specific
    `kafka_unavailable` error, not the generic -32603 mask. And it
    must not leak the producer object (the `stop()` path always
    runs). This is the regression class that broke the mcp compose
    service in v0.4.0 → v0.4.2 when KAFKA_BOOTSTRAP_SERVERS was
    unset."""
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)

    producer = AsyncMock()
    producer.start = AsyncMock(side_effect=OSError("broker down"))
    producer.stop = AsyncMock()
    producer.send_and_wait = AsyncMock()

    try:
        with patch("aiokafka.AIOKafkaProducer", return_value=producer):
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
            ) as ac:
                token = await _token(
                    db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
                )
                body = await _call(
                    ac,
                    token,
                    "poison_message",
                    {"topic": "job.submitted", "payload": {}},
                )
        assert body["error"]["code"] == protocol.MCP_TOOL_ERROR
        assert body["error"]["data"]["error_code"] == "kafka_unavailable"
        # Cleanup path ran despite start() raising
        producer.stop.assert_awaited()
    finally:
        teardown()


# ---------------------------------------------------------------------------
# Scope enforcement — one representative check per tool is overkill;
# ADR-0007's dispatch layer already covers this. Do it once against
# `saturate_redis` to prove the framework routes chaos denials into
# `chaos.tool_denied` (see PR B's tests for the same shape).
# ---------------------------------------------------------------------------


async def test_saturate_redis_missing_chaos_scope_is_forbidden(
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
            body = await _call(
                ac,
                token,
                "saturate_redis",
                {"num_keys": 5, "value_bytes": 32},
            )
        assert body["error"]["code"] == protocol.MCP_FORBIDDEN
    finally:
        teardown()


# ---------------------------------------------------------------------------
# create_bad_data_job — the persistent-bug chaos hook (v0.4.0)
# ---------------------------------------------------------------------------


async def test_create_bad_data_job_inserts_human_required_dlq_entry(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The synthetic DLQ row lands with `remediation_hint=human_required`
    so `replay_dlq_by_category` refuses to touch it — that's exactly the
    branch the agent's escalate-not-replay path exercises."""
    from app.models.enums import RemediationHint
    from app.models.job import Job
    from sqlalchemy import select as _select

    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            token = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            payload = _content(
                await _call(
                    ac, token, "create_bad_data_job", {}
                )
            )
        assert payload["accepted"] is True
        assert (
            payload["remediation_hint"]
            == RemediationHint.HUMAN_REQUIRED.value
        )
        rows = (
            await db_session.execute(
                _select(Job).where(Job.id == uuid.UUID(payload["job_id"]))
            )
        ).scalars().all()
        assert rows
        job = rows[0]
        assert job.status == "dead_letter"
        assert (
            job.remediation_hint == RemediationHint.HUMAN_REQUIRED.value
        )
    finally:
        teardown()


async def test_seed_dlq_messages_creates_declared_rows(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """Platform half of commander ADR 0010: a scenario declares the DLQ
    content it is graded against instead of inheriting a standing pool."""
    from app.models.enums import RemediationHint
    from app.models.job import Job
    from sqlalchemy import select as _select

    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            token = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            payload = _content(
                await _call(
                    ac,
                    token,
                    "seed_dlq_messages",
                    {
                        "remediation_hint": RemediationHint.WAIT_AND_REPLAY.value,
                        "count": 3,
                        "job_type": "bulk_api_sync",
                    },
                )
            )
        assert payload["accepted"] is True
        assert payload["count"] == 3
        assert len(payload["job_ids"]) == 3

        rows = (
            await db_session.execute(
                _select(Job).where(
                    Job.id.in_([uuid.UUID(i) for i in payload["job_ids"]])
                )
            )
        ).scalars().all()
        assert len(rows) == 3
        for job in rows:
            assert job.status == "dead_letter"
            assert (
                job.remediation_hint == RemediationHint.WAIT_AND_REPLAY.value
            )
            # Tagged so the reset deletes rather than cancels them.
            assert job.payload.get("seeded_fixture") is True
    finally:
        teardown()


async def test_seed_dlq_messages_rejects_unknown_hint(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """An unrecognised hint must fail loudly — a row with a bogus hint
    would silently never match the agent's category filters."""
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            token = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            body = await _call(
                ac,
                token,
                "seed_dlq_messages",
                {"remediation_hint": "definitely_not_a_hint"},
            )
        assert "error" in body
    finally:
        teardown()


async def test_create_bad_data_job_lazy_creates_chaos_owner_in_caller_tenant(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """FIX_PLAN #8: when the caller's tenant has no users, chaos must
    lazy-create a user IN THE CALLER'S TENANT — never fall back to a
    user from another tenant (violates ADR 0003 isolation). Verified
    by creating a fresh tenant with zero users and asserting the job's
    user_id ends up in that same tenant."""
    from app.models.job import Job
    from app.models.tenant import Tenant
    from app.models.user import User
    from sqlalchemy import select as _select

    fresh_tenant = Tenant(
        slug=f"chaos-t-{uuid.uuid4().hex[:8]}",
        name="Chaos-only tenant",
        is_active=True,
    )
    db_session.add(fresh_tenant)
    await db_session.flush()
    assert fresh_tenant.id != default_tenant.id
    # Pre-condition: no users in this tenant.
    pre_users = (
        await db_session.execute(
            _select(User).where(User.tenant_id == fresh_tenant.id)
        )
    ).scalars().all()
    assert pre_users == []

    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            token = await _token(
                db_session, fresh_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            payload = _content(
                await _call(ac, token, "create_bad_data_job", {})
            )
        assert payload["accepted"] is True
        job = (
            await db_session.execute(
                _select(Job).where(Job.id == uuid.UUID(payload["job_id"]))
            )
        ).scalar_one()
        # The load-bearing invariant: job.tenant_id and the owning
        # user.tenant_id must match. Pre-v0.4.6 this would have been
        # violated (job goes to fresh_tenant, user pulled from default).
        assert job.tenant_id == fresh_tenant.id
        owner = (
            await db_session.execute(
                _select(User).where(User.id == job.user_id)
            )
        ).scalar_one()
        assert owner.tenant_id == fresh_tenant.id
        # Chaos user is marked inactive so it doesn't show up in
        # operator-facing user lists.
        assert owner.is_active is False
        assert owner.email.startswith("chaos-owner")

        # Second call in the same tenant reuses the same chaos user
        # (idempotent) — no proliferation of chaos-owner rows.
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            second = _content(
                await _call(ac, token, "create_bad_data_job", {})
            )
        second_job = (
            await db_session.execute(
                _select(Job).where(Job.id == uuid.UUID(second["job_id"]))
            )
        ).scalar_one()
        assert second_job.user_id == owner.id
        chaos_owners = (
            await db_session.execute(
                _select(User).where(User.tenant_id == fresh_tenant.id)
            )
        ).scalars().all()
        assert len(chaos_owners) == 1
    finally:
        teardown()


# ---------------------------------------------------------------------------
# create_stale_cache — hot_set chaos hook (v0.4.7 / FIX_PLAN #24 item 2)
# ---------------------------------------------------------------------------


async def test_create_stale_cache_not_registered_when_chaos_disabled(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """Same gating story as saturate_redis — chaos-only tool must not
    appear in the registry when CHAOS_ENABLED=false."""
    from app.mcp.standalone import create_mcp_app

    redis_stub = _RedisStub()
    app = create_mcp_app()

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
        token = await _token(
            db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
        )
        body = await _call(ac, token, "create_stale_cache", {})
    assert body["error"]["code"] == protocol.MCP_TOOL_NOT_FOUND


async def test_create_stale_cache_populates_default_hot_set_key(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """Default key is the hot_set the `remediate_stale_cache_success`
    scenario reads, value is a JSON array of fabricated IDs, TTL
    passed to Redis."""
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            token = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            payload = _content(
                await _call(ac, token, "create_stale_cache", {})
            )
        assert payload["accepted"] is True
        assert payload["key"] == "cache:jobs:worker-dispatcher:hot_set"
        assert payload["ttl_seconds"] == 600
        # The stub captured the write — value is JSON array with 3 fake IDs.
        raw = redis_stub._store["cache:jobs:worker-dispatcher:hot_set"]
        stale = json.loads(raw if isinstance(raw, str) else raw.decode())
        assert isinstance(stale, list)
        assert len(stale) == 3
        assert all(s.startswith("stale-fixture-") for s in stale)
    finally:
        teardown()


async def test_create_stale_cache_refuses_key_outside_allowlist(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """Key must be under a prefix that `invalidate_cache_key` accepts —
    otherwise the compensator would refuse to clear it and the
    round-trip is broken."""
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            token = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            body = await _call(
                ac,
                token,
                "create_stale_cache",
                {"key": "arbitrary:foo:bar"},
            )
        assert body["error"]["code"] == protocol.MCP_TOOL_ERROR
        assert body["error"]["data"]["error_code"] == "stale_cache_key_forbidden"
    finally:
        teardown()


async def test_create_stale_cache_round_trip_with_invalidate_cache_key(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """The load-bearing contract test per ADR 0008 amendment: every
    chaos hook must name a compensator + link a round-trip test.
    Sequence:
      1. create_stale_cache populates the hot_set key.
      2. invalidate_cache_key clears it.
      3. Redis stub confirms the key is gone.
    If this fails, the `remediate_stale_cache_success` scenario is
    unwinnable — the compensator can't undo what the chaos hook did."""
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            # Step 1: chaos scope populates the key.
            chaos_token = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            create = _content(
                await _call(
                    ac, chaos_token, "create_stale_cache", {}
                )
            )
            assert create["accepted"] is True
            key = create["key"]
            assert key in redis_stub._store  # actually there

            # Step 2: actions scope invalidates it — same key.
            actions_token = await _token(
                db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
            )
            invalidate = _content(
                await _call(
                    ac,
                    actions_token,
                    "invalidate_cache_key",
                    {"key": key, "idempotency_key": "stale-round-trip-1"},
                )
            )
            # Step 3: compensator observed + cleared the key.
            assert invalidate["deleted"] is True
            assert key not in redis_stub._store
    finally:
        teardown()
