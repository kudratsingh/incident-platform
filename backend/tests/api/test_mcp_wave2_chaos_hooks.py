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
        from app.mcp.tools import consumer_lag as _cl
        from app.mcp.tools import list_active_alerts as _laa

        importlib.reload(_cl)
        importlib.reload(_laa)

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
