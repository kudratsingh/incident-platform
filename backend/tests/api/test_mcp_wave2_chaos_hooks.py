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

import pytest
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
        importlib.reload(chaos_pkg.create_mislabeled_dlq_job)  # type: ignore[attr-defined]
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


async def test_saturate_redis_refuses_a_footprint_above_the_cap(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """Each dimension was bounded and their product was not, so the maxima
    multiplied out to ~100 GB against the Redis every scenario shares — an OOM
    rather than the memory pressure the tool is for (WO-R2-56). Both values
    below are individually legal.
    """
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
                "saturate_redis",
                {"num_keys": 100_000, "value_bytes": 1_048_576},
            )
        assert body["error"]["code"] == protocol.JSONRPC_INVALID_PARAMS
        # Refused before writing: a partial 100 GB is still a dead Redis.
        assert redis_stub._store == {}
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


async def test_poison_message_writes_an_unclassified_schema_dlq_entry(
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    test_user,  # type: ignore[no-untyped-def]
) -> None:
    """WO-R2-166 — the defaults, read back off the row.

    The synthetic DLQ row is the observable side of the hook: real
    consumers log-and-drop schema errors, so without it the agent's
    remediation loop has nothing to react to. What that row says has now
    been wrong twice. It shipped as `replay_safe` beside a
    `SchemaValidationError` (WO-R2-146's live defect — the agent read it
    right and was graded wrong), then as `replay_safe` beside an
    `UpstreamTimeout` text, which passed the coherence screen while
    describing a transient fault this hook never injects.

    The hint moves instead of the text: a poisoned message is not safe to
    replay, and a fresh one has been classified by nobody, so the default
    row is NULL-hint with the schema-violation text it earned.
    """
    from app.lab.dlq_failure_stories import coherence_violations
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
        assert payload["created"] is True
        assert payload["fixture_name"] == "poison-message"
        # Reported as null, not as a category the platform has not assigned.
        assert payload["remediation_hint"] is None
        row = (
            await db_session.execute(
                _select(Job).where(
                    Job.id == uuid.UUID(payload["dlq_job_id"])
                )
            )
        ).scalar_one()
        assert row.status == "dead_letter"
        assert row.tenant_id == default_tenant.id
        # The heart of it: nothing on this row says a replay is the fix.
        assert row.remediation_hint is None
        assert row.error_message is not None
        assert "SchemaValidationError" in row.error_message
        assert "missing required field" in row.error_message
        # A permanent-fault text under a null hint is coherent — the hint is
        # a classification, the text is a symptom, and neither invites a
        # replay (see `app.lab.dlq_failure_stories`).
        assert not coherence_violations(None, row.error_message), (
            row.error_message
        )
        # Still traceable to this hook and this topic.
        assert "job.submitted" in row.error_message
        assert "poison_message" in row.error_message
        # Declared fixture: the reset DELETEs it rather than cancelling it.
        assert row.payload["seeded_fixture"] is True
        assert row.payload["chaos_fixture"] == "poison_message"
        assert row.payload["fixture_name"] == "poison-message"
        # The Kafka half is untouched by any of this — the injection is real.
        producer.send_and_wait.assert_awaited_once()
    finally:
        teardown()


async def test_poison_message_can_seed_the_row_already_human_required(
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    test_user,  # type: ignore[no-untyped-def]
) -> None:
    """The other declarable hint, for a scenario that wants the row already
    categorised so `replay_dlq_by_category` refuses it on sight and the
    escalate-not-replay branch is reachable without a triage step.

    Same text either way: this hook injects one kind of fault, and the only
    thing the argument changes is whether anything has classified it.
    """
    from app.lab.dlq_failure_stories import coherence_violations
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
                        {
                            "topic": "job.progress",
                            "payload": {},
                            "fixture_name": "poison-classified",
                            "remediation_hint": "human_required",
                        },
                    )
                )
        assert payload["remediation_hint"] == (
            RemediationHint.HUMAN_REQUIRED.value
        )
        row = (
            await db_session.execute(
                _select(Job).where(Job.id == uuid.UUID(payload["dlq_job_id"]))
            )
        ).scalar_one()
        assert row.remediation_hint == RemediationHint.HUMAN_REQUIRED.value
        assert row.error_message is not None
        assert "SchemaValidationError" in row.error_message
        assert "job.progress" in row.error_message
        assert not coherence_violations(
            RemediationHint.HUMAN_REQUIRED.value, row.error_message
        ), row.error_message
    finally:
        teardown()


async def test_poison_message_refuses_a_replay_safe_hint_over_the_wire(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """The refusal an agent actually meets. `replay_safe` is not in the
    hook's vocabulary, so the envelope rejects it as invalid input — and
    nothing is published, because validation runs before the handler."""
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
                body = await _call(
                    ac,
                    token,
                    "poison_message",
                    {
                        "topic": "job.submitted",
                        "remediation_hint": "replay_safe",
                    },
                )
        assert "error" in body, body
        producer.send_and_wait.assert_not_awaited()
    finally:
        teardown()


async def test_poison_message_id_is_derived_from_tenant_and_fixture_name(
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    test_user,  # type: ignore[no-untyped-def]
) -> None:
    """A scenario pins this id in YAML before the hook runs, so the grader
    can assert *which* row the agent acted on (commander cmd #187). The
    recipe is exported rather than transcribed, and it is per-tenant: the
    idempotency probe is RLS-scoped, so a foreign row under the same name
    would be invisible to it and the INSERT would collide on the primary
    key — a 500 where the contract promises a 409.

    Also pins that the namespace differs from `create_bad_data_job`'s, which
    is the property that lets both hooks use the same `fixture_name`.
    """
    from app.mcp.tools.chaos.create_bad_data_job import (
        fixture_id as bad_data_fixture_id,
    )
    from app.mcp.tools.chaos.poison_message import fixture_id

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
                        {
                            "topic": "job.submitted",
                            "fixture_name": "shared-name",
                        },
                    )
                )
        expected = fixture_id(default_tenant.id, "shared-name")
        assert payload["dlq_job_id"] == str(expected)
        # Same name, different hook, different row.
        assert fixture_id(default_tenant.id, "shared-name") != (
            bad_data_fixture_id(default_tenant.id, "shared-name")
        )
        # Same name, different tenant, different row.
        assert fixture_id(uuid.uuid4(), "shared-name") != expected
    finally:
        teardown()


async def test_poison_message_repeat_is_idempotent_until_it_drifts(
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    test_user,  # type: ignore[no-untyped-def]
) -> None:
    """Three properties in one round trip, because they only make sense
    together:

    1. A repeat that finds its row intact reports `created=False` and does
       not manufacture a second row.
    2. It still publishes. The Kafka half is a verb, not a fixture — every
       accepted call really does put another poisoned message on the topic,
       and the description says so.
    3. Once the row has drifted the hook refuses, and refuses *before* the
       producer starts. A drifted row is the run's evidence (something
       fenced it, or a replay moved it), and rewriting it would hand the
       next run a pre-remediated world and grade it clean.
    """
    from app.models.enums import RemediationHint
    from app.models.job import Job
    from sqlalchemy import select as _select

    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)

    producer = AsyncMock()
    producer.start = AsyncMock()
    producer.stop = AsyncMock()
    producer.send_and_wait = AsyncMock()

    args = {"topic": "job.submitted", "fixture_name": "repeat-probe"}
    try:
        with patch("aiokafka.AIOKafkaProducer", return_value=producer):
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
            ) as ac:
                token = await _token(
                    db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
                )
                first = _content(
                    await _call(ac, token, "poison_message", args)
                )
                second = _content(
                    await _call(ac, token, "poison_message", args)
                )

                assert first["created"] is True
                assert second["created"] is False
                assert second["dlq_job_id"] == first["dlq_job_id"]
                rows = (
                    await db_session.execute(
                        _select(Job).where(
                            Job.payload["fixture_name"].as_string()
                            == "repeat-probe"
                        )
                    )
                ).scalars().all()
                assert len(rows) == 1
                # Two accepted calls, two poisoned messages.
                assert producer.send_and_wait.await_count == 2

                # Now drift it the way a fence would, and re-ask.
                rows[0].remediation_hint = (
                    RemediationHint.HUMAN_REQUIRED.value
                )
                await db_session.flush()
                producer.reset_mock()
                body = await _call(ac, token, "poison_message", args)

        assert body["error"]["code"] == protocol.MCP_TOOL_ERROR
        assert body["error"]["data"]["error_code"] == (
            "poison_fixture_name_in_use"
        )
        # The refusal came before the broker: a call that will be refused
        # must not have poisoned a topic on its way to the refusal.
        producer.start.assert_not_awaited()
        producer.send_and_wait.assert_not_awaited()
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
    """Omitting `remediation_hint` keeps the pre-v0.6.2 behaviour: the row
    lands `human_required` so `replay_dlq_by_category` refuses to touch it
    — the branch the agent's escalate-not-replay path exercises.

    Also pins the two things WO-R2-158 added around it: the id is derived
    from the tenant and `fixture_name` rather than random, and the row is
    tagged as a declared fixture so the reset DELETEs it.
    """
    from app.lab.dlq_failure_stories import story
    from app.mcp.tools.chaos.create_bad_data_job import fixture_id
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
        assert payload["created"] is True
        assert (
            payload["remediation_hint"]
            == RemediationHint.HUMAN_REQUIRED.value
        )
        # Deterministic and pinnable: the caller could have computed this
        # before invoking, which is what lets a scenario name the row it
        # grades in YAML written before the run.
        assert payload["fixture_name"] == "bad-data-job"
        assert payload["job_id"] == str(
            fixture_id(default_tenant.id, "bad-data-job")
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
        assert job.error_message == story("csv_bad_row").error_message
        # Declared scaffolding: `_delete_seeded_dlq_fixtures` DELETEs on the
        # marker, and `chaos_fixture` stays for provenance.
        assert job.payload["seeded_fixture"] is True
        assert job.payload["chaos_fixture"] == "bad_data_job"
        assert job.payload["fixture_name"] == "bad-data-job"
    finally:
        teardown()


@pytest.mark.parametrize("declared", ["unclassified", None])
async def test_create_bad_data_job_can_leave_the_row_unclassified(
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    test_user,  # type: ignore[no-untyped-def]
    declared: str | None,
) -> None:
    """WO-R2-158 / the `dlq_human_required_escalates` drill.

    Seeded pre-classified, the fence the drill grades is a value the row
    already has. With `remediation_hint` unclassified the row arrives with
    a NULL hint and a bad-data error text, so the agent has to read the
    error, conclude a replay cannot fix a bad row in the stored payload,
    and raise the fence itself.

    Both spellings are tested because a scenario file that means an empty
    hint naturally writes `null`, while the inputSchema an agent reads is
    clearer as a word. Omitting the field is a third thing and must NOT
    land here — that is the test above.
    """
    from app.lab.dlq_failure_stories import coherence_violations, story
    from app.mcp.tools.chaos.create_bad_data_job import fixture_id
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
                    "create_bad_data_job",
                    {
                        "remediation_hint": declared,
                        "fixture_name": "unfenced-csv",
                    },
                )
            )
        assert payload["accepted"] is True
        assert payload["remediation_hint"] is None
        assert payload["job_id"] == str(
            fixture_id(default_tenant.id, "unfenced-csv")
        )

        job = (
            await db_session.execute(
                _select(Job).where(Job.id == uuid.UUID(payload["job_id"]))
            )
        ).scalar_one()
        assert job.status == "dead_letter"
        assert job.remediation_hint is None, (
            "an unclassified row is the whole point — a stamped hint makes "
            "the agent's fence a no-op the eval cannot see"
        )
        # The error text is what a human would read, and it is the declared
        # table entry rather than a string composed here.
        assert (
            job.error_message
            == story("unclassified_csv_bad_row").error_message
        )
        assert "invalid literal for int()" in job.error_message
        assert "row 8,214" in job.error_message
        # And the pair is coherent by the lab's own screen: nothing has
        # classified the row, and the text does not invite a replay.
        assert not coherence_violations(
            job.remediation_hint, job.error_message
        )
        # Nothing has fenced it yet — that is the state the drill starts in.
        assert job.fenced_at is None
        assert job.fenced_by is None
        assert job.payload["seeded_fixture"] is True
    finally:
        teardown()


async def test_create_bad_data_job_repeat_is_idempotent_until_it_drifts(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """Same contract as `create_stuck_dag`: a repeat that finds the row
    still matching is a no-op; once it has drifted the hook refuses rather
    than rewriting a row that is now evidence.

    The load-bearing drift here is the *hint*, not the status. A fence is
    exactly what the drill measures, so silently returning `created=False`
    over a fenced row would hand the next run a pre-fenced world and grade
    it clean.
    """
    from app.models.enums import RemediationHint
    from app.models.job import Job
    from sqlalchemy import select as _select

    args = {
        "remediation_hint": "unclassified",
        "fixture_name": "unfenced-csv",
    }
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
            first = _content(
                await _call(ac, token, "create_bad_data_job", args)
            )
            assert first["created"] is True

            repeat = _content(
                await _call(ac, token, "create_bad_data_job", args)
            )
            assert repeat["created"] is False
            assert repeat["job_id"] == first["job_id"]

            # Declaring a different hint over the same name is drift too:
            # returning the stored row would report a fixture the caller
            # did not ask for.
            mismatched = await _call(
                ac,
                token,
                "create_bad_data_job",
                {**args, "remediation_hint": "human_required"},
            )
            assert (
                mismatched["error"]["data"]["error_code"]
                == "bad_data_fixture_name_in_use"
            )

            # Now fence the row the way the agent would, and re-seed.
            job = (
                await db_session.execute(
                    _select(Job).where(
                        Job.id == uuid.UUID(first["job_id"])
                    )
                )
            ).scalar_one()
            job.remediation_hint = RemediationHint.HUMAN_REQUIRED.value
            await db_session.flush()

            after_fence = await _call(
                ac, token, "create_bad_data_job", args
            )
        assert (
            after_fence["error"]["data"]["error_code"]
            == "bad_data_fixture_name_in_use"
        )
        message = json.dumps(after_fence["error"])
        assert "human_required" in message
        assert "reset the environment" in message
    finally:
        teardown()


async def test_create_bad_data_job_ids_are_scoped_to_the_calling_tenant(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """The tenant is in the uuid5 key so two tenants can drill the same
    `fixture_name` concurrently.

    Without it the second tenant's INSERT would collide on a primary key
    its RLS-scoped probe cannot see — a 500 where the contract promises a
    409. Same reasoning as `create_stuck_dag`'s per-tenant chain ids
    (WO-R2-55); widening the probe past RLS would be the wrong repair.
    """
    from app.mcp.tools.chaos.create_bad_data_job import fixture_id
    from app.models.job import Job
    from app.models.tenant import Tenant
    from sqlalchemy import select as _select

    other = Tenant(
        slug=f"chaos-t-{uuid.uuid4().hex[:8]}",
        name="Second driller",
        is_active=True,
    )
    db_session.add(other)
    await db_session.flush()

    assert fixture_id(default_tenant.id, "same-name") != fixture_id(
        other.id, "same-name"
    )

    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        ids = []
        for tenant_id in (default_tenant.id, other.id):
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
            ) as ac:
                token = await _token(
                    db_session, tenant_id, [Scope.CHAOS_INVOKE.value]
                )
                payload = _content(
                    await _call(
                        ac,
                        token,
                        "create_bad_data_job",
                        {"fixture_name": "same-name"},
                    )
                )
            assert payload["created"] is True
            ids.append(payload["job_id"])
        assert ids[0] != ids[1]
        rows = (
            await db_session.execute(
                _select(Job).where(
                    Job.id.in_([uuid.UUID(i) for i in ids])
                )
            )
        ).scalars().all()
        assert {r.tenant_id for r in rows} == {default_tenant.id, other.id}
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

        # Second call in the same tenant is an idempotent repeat: same
        # deterministic id, `created=False`, and no proliferation of
        # chaos-owner rows. (Before WO-R2-158 this inserted a second,
        # randomly-idded row that merely happened to reuse the owner.)
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            second = _content(
                await _call(ac, token, "create_bad_data_job", {})
            )
        assert second["created"] is False
        assert second["job_id"] == payload["job_id"]
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
# create_mislabeled_dlq_job — the one sanctioned incoherent row (WO-R2-166)
# ---------------------------------------------------------------------------


async def test_create_mislabeled_dlq_job_writes_the_incoherent_pair(
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    test_user,  # type: ignore[no-untyped-def]
) -> None:
    """The fixture for "the classifier lied": hint `replay_safe`, text a
    permanent bad-data fault. The row is supposed to contradict itself, so
    this test asserts the contradiction is really there AND that the
    coherence screen still reports it.

    That second half is the load-bearing one. Every other lab row is held
    to the rule that a text must match the action its hint prescribes
    (WO-R2-146). If the screen ever stopped flagging this row, the sanctioned
    exception would have become a hole in the rule and the original defect
    could walk back in through it.
    """
    from app.lab.dlq_failure_stories import coherence_violations
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
                    "create_mislabeled_dlq_job",
                    {"mislabel": True},
                )
            )
        assert payload["accepted"] is True
        assert payload["created"] is True
        assert payload["remediation_hint"] == RemediationHint.REPLAY_SAFE.value
        row = (
            await db_session.execute(
                _select(Job).where(Job.id == uuid.UUID(payload["job_id"]))
            )
        ).scalar_one()
        assert row.status == "dead_letter"
        assert row.tenant_id == default_tenant.id
        # The label…
        assert row.remediation_hint == RemediationHint.REPLAY_SAFE.value
        # …and the text that contradicts it.
        assert row.error_message is not None
        assert "invalid literal for int()" in row.error_message
        assert row.error_message == payload["error_message"]
        # The screen must still call this out. Asserting the reason, not just
        # that there is one, so a screen that flagged it for some unrelated
        # wording change would not satisfy this test.
        reasons = coherence_violations(
            row.remediation_hint, row.error_message
        )
        assert reasons, (
            "the deliberately mislabelled row reads as coherent — the "
            "sanctioned exception has become a loophole"
        )
        assert any("permanent data fault" in r for r in reasons), reasons
        # Declared fixture: DELETEd by the reset, not left cancelled.
        assert row.payload["seeded_fixture"] is True
        assert row.payload["chaos_fixture"] == "mislabeled_dlq_job"
    finally:
        teardown()


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({}, id="omitted"),
        pytest.param({"mislabel": False}, id="false"),
        pytest.param(
            {"fixture_name": "sneaky"}, id="other-args-without-the-flag"
        ),
    ],
)
async def test_create_mislabeled_dlq_job_needs_the_explicit_flag(
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    arguments: dict[str, Any],
) -> None:
    """The second gate (the tool's name is the first). `mislabel` has no
    default and accepts only `true`, so an incoherent row can never be the
    result of a call that did not say what it was asking for — and there is
    no coherent row this tool could fall back to writing.
    """
    from app.models.job import Job
    from sqlalchemy import func as _func
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
            body = await _call(
                ac, token, "create_mislabeled_dlq_job", arguments
            )
        assert "error" in body, body
        # Refused before any write.
        written = (
            await db_session.execute(
                _select(_func.count()).select_from(Job).where(
                    Job.payload["chaos_fixture"].as_string()
                    == "mislabeled_dlq_job"
                )
            )
        ).scalar_one()
        assert written == 0
    finally:
        teardown()


async def test_create_mislabeled_dlq_job_id_is_deterministic_and_distinct(
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    test_user,  # type: ignore[no-untyped-def]
) -> None:
    """A scenario pins this id before the hook runs, and the grading here
    is mostly "the agent left this exact row alone" — so the id has to be
    computable in advance and must not collide with a sibling hook's row
    under the same `fixture_name`."""
    from app.mcp.tools.chaos.create_bad_data_job import (
        fixture_id as bad_data_fixture_id,
    )
    from app.mcp.tools.chaos.create_mislabeled_dlq_job import fixture_id
    from app.mcp.tools.chaos.poison_message import (
        fixture_id as poison_fixture_id,
    )

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
                    "create_mislabeled_dlq_job",
                    {"mislabel": True, "fixture_name": "shared-name"},
                )
            )
        expected = fixture_id(default_tenant.id, "shared-name")
        assert payload["job_id"] == str(expected)
        assert payload["fixture_name"] == "shared-name"
        assert expected != bad_data_fixture_id(
            default_tenant.id, "shared-name"
        )
        assert expected != poison_fixture_id(
            default_tenant.id, "shared-name"
        )
        assert expected != fixture_id(uuid.uuid4(), "shared-name")
    finally:
        teardown()


async def test_create_mislabeled_dlq_job_repeat_is_idempotent_until_it_drifts(
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    test_user,  # type: ignore[no-untyped-def]
) -> None:
    """On this fixture a drifted row is the most interesting thing in the
    run — it means the agent believed the label and replayed a row whose
    text says the payload is broken. Overwriting it would destroy the
    result, so the hook refuses instead."""
    from app.models.enums import JobStatus
    from app.models.job import Job
    from sqlalchemy import select as _select

    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    args = {"mislabel": True, "fixture_name": "drift-probe"}
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            token = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            first = _content(
                await _call(ac, token, "create_mislabeled_dlq_job", args)
            )
            second = _content(
                await _call(ac, token, "create_mislabeled_dlq_job", args)
            )
            assert first["created"] is True
            assert second["created"] is False
            assert second["job_id"] == first["job_id"]

            # A replay is what moves it out of dead_letter.
            row = (
                await db_session.execute(
                    _select(Job).where(Job.id == uuid.UUID(first["job_id"]))
                )
            ).scalar_one()
            row.status = JobStatus.PENDING.value
            await db_session.flush()
            body = await _call(
                ac, token, "create_mislabeled_dlq_job", args
            )
        assert body["error"]["code"] == protocol.MCP_TOOL_ERROR
        assert body["error"]["data"]["error_code"] == (
            "mislabeled_fixture_name_in_use"
        )
        # The refusal names the cause, not just the code — a builder reading
        # it should not have to guess which drift fired.
        rendered = json.dumps(body["error"])
        assert "something replayed it" in rendered
        assert "never rewrites it" in rendered
    finally:
        teardown()


async def test_create_mislabeled_dlq_job_not_registered_when_chaos_disabled(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """ADR 0008 gate 1, on the newest chaos tool. A hook that can write a
    deliberately misleading row is exactly the kind that must be absent
    from `tools/list` on a stack with chaos off."""
    from app.mcp.registry import get_tool

    assert get_tool("create_mislabeled_dlq_job") is None


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


async def test_create_stale_cache_refuses_the_live_job_read_cache_key(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """R2-20: `cache:` is a prefix of the platform's live per-job read
    cache `cache:job:{tenant}:{job}`. Writing this hook's JSON array
    there breaks `GET /jobs/{id}` for real users until the TTL lapses.
    The hook must refuse the key before any Redis call, the same way
    `get_cache_key_info` refuses a namespace it does not own."""
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    live_key = f"cache:job:{default_tenant.id}:{uuid.uuid4()}"
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            token = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            body = await _call(
                ac, token, "create_stale_cache", {"key": live_key}
            )
        assert body["error"]["code"] == protocol.MCP_TOOL_ERROR
        assert body["error"]["data"]["error_code"] == "stale_cache_key_forbidden"
        # Refused *before* Redis — nothing was written.
        assert live_key not in redis_stub._store
    finally:
        teardown()


async def test_poison_message_lazy_creates_chaos_owner_on_unseeded_tenant(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """R2-16: the synthetic DLQ row is `poison_message`'s only observable
    effect (real consumers log-and-drop schema errors). On a tenant with
    no users the hook used to skip the row and still answer
    accepted=true, leaving the scenario unwinnable. Both sibling hooks
    lazy-create a chaos owner for exactly this case — so must this one."""
    from app.models.job import Job
    from app.models.tenant import Tenant
    from app.models.user import User
    from sqlalchemy import select as _select

    fresh_tenant = Tenant(
        slug=f"poison-t-{uuid.uuid4().hex[:8]}",
        name="Chaos-only tenant",
        is_active=True,
    )
    db_session.add(fresh_tenant)
    await db_session.flush()
    pre_users = (
        await db_session.execute(
            _select(User).where(User.tenant_id == fresh_tenant.id)
        )
    ).scalars().all()
    assert pre_users == []

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
                    db_session, fresh_tenant.id, [Scope.CHAOS_INVOKE.value]
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
        job = (
            await db_session.execute(
                _select(Job).where(Job.id == uuid.UUID(payload["dlq_job_id"]))
            )
        ).scalar_one()
        assert job.tenant_id == fresh_tenant.id
        owner = (
            await db_session.execute(_select(User).where(User.id == job.user_id))
        ).scalar_one()
        # Same chaos owner the sibling hooks create — so the reset's
        # `_delete_chaos_owner_users` sweep reaches this one too.
        assert owner.tenant_id == fresh_tenant.id
        assert owner.email.startswith("chaos-owner")
        assert owner.is_active is False
    finally:
        teardown()


async def test_poison_message_send_failure_returns_typed_broker_error(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """R2-16: only `start()` was inside the broad catch, so a
    `send_and_wait` failure (unknown topic, no leader, auth) escaped as
    an opaque -32603 the ChaosClient buckets as a transport fault. It
    needs its own code — `kafka_unavailable` would be a misnomer for a
    broker that answered and rejected the send."""
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    producer = AsyncMock()
    producer.start = AsyncMock()
    producer.stop = AsyncMock()
    producer.send_and_wait = AsyncMock(side_effect=OSError("unknown topic"))
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
        assert body["error"]["data"]["error_code"] == "kafka_send_failed"
        producer.stop.assert_awaited()
    finally:
        teardown()


async def test_seed_dlq_messages_unknown_hint_is_a_validation_error(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """R2-16: strengthens `test_seed_dlq_messages_rejects_unknown_hint`.
    A bare `ValueError` reached the client as -32603 `internal tool
    error` and logged `mcp tool crashed` — indistinguishable from a real
    platform fault. Constraining the field on the input model rejects it
    at parse time with -32602 instead, and the enumerated values become
    visible in the tool's inputSchema."""
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
            listing = await ac.post(
                "/mcp",
                json=_rpc("tools/list"),
                headers={"Authorization": f"Bearer {token}"},
            )
        assert body["error"]["code"] == protocol.JSONRPC_INVALID_PARAMS
        assert body["error"]["code"] != protocol.JSONRPC_INTERNAL_ERROR
        # The agent can now discover the valid values without a failed call.
        tools = {t["name"]: t for t in listing.json()["result"]["tools"]}
        schema = tools["seed_dlq_messages"]["inputSchema"]
        assert "replay_safe" in json.dumps(schema["properties"]["remediation_hint"])
    finally:
        teardown()


def test_seed_dlq_hint_literal_matches_the_enum() -> None:
    """R2-16: the hint values are spelled out on the input model so they
    reach the agent through the tool's inputSchema. That copy can drift
    from `RemediationHint` — if the enum gains a member the tool would
    silently refuse a legitimate hint. Pin the two together."""
    import typing

    from app.mcp.tools.chaos.seed_dlq_messages import _HINT_VALUES
    from app.models.enums import RemediationHint

    assert set(typing.get_args(_HINT_VALUES)) == {
        h.value for h in RemediationHint
    }


@pytest.mark.parametrize(
    "hint",
    ["replay_safe", "wait_and_replay", "human_required"],
)
async def test_seeded_dlq_row_text_agrees_with_its_hint(
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    test_user,  # type: ignore[no-untyped-def]
    hint: str,
) -> None:
    """WO-R2-146, end to end for the declared-fixture hook.

    A scenario that declares only a hint gets the canned text for it,
    and the agent reads both fields off the same row. This hook's table
    used to pair `replay_safe` with a SchemaValidationError, which is a
    permanent data fault — an agent following the error refuses the
    replay the hint asks for, and the scenario grades it wrong.

    Read back through the wire rather than off the table, so a hook that
    stopped consulting the table fails here.
    """
    from app.lab.dlq_failure_stories import coherence_violations
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
                    {"remediation_hint": hint, "count": 1},
                )
            )
        row = (
            await db_session.execute(
                _select(Job).where(
                    Job.id == uuid.UUID(payload["job_ids"][0])
                )
            )
        ).scalar_one()
        assert row.remediation_hint == hint
        assert row.error_message is not None
        assert not coherence_violations(hint, row.error_message), (
            row.error_message
        )
    finally:
        teardown()
