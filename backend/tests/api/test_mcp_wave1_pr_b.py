"""End-to-end MCP tests for Wave 1 PR B — chaos + alerts tools.

Reuses the pattern from test_mcp_standalone.py (Redis stub, minted SA
token). Focused on the new tools:
  - `kill_consumer` (chaos, requires chaos:invoke) — only visible when
    CHAOS_ENABLED=true. Sets the expected Redis kill key.
  - `list_active_alerts` (incidents:read) — returns rows for the
    caller's tenant only.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import patch

import pytest_asyncio
from app.config import Settings
from app.core.scopes import Scope
from app.dependencies import get_db, get_redis
from app.models.alert import Alert
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
    def __init__(self) -> None:
        self._store: dict[str, bytes | str] = {}

    async def get(self, key: str) -> bytes | str | None:
        return self._store.get(key)

    async def set(
        self, key: str, value: bytes | str, ex: int | None = None
    ) -> bool:
        # ex is captured just so callers can assert on it via mocks
        self._store[key] = value
        return True


def _mcp_app_with_chaos_enabled(db_session: AsyncSession, redis_stub: _RedisStub):
    """Build a fresh MCP app under CHAOS_ENABLED=true so chaos tools
    register. Patches settings before create_mcp_app runs."""
    with patch(
        "app.mcp.standalone.assert_chaos_gate", lambda *a, **kw: None
    ), patch(
        "app.mcp.chaos.get_settings",
        return_value=Settings(chaos_enabled=True, environment="test"),
    ):
        # Reload the chaos tools module so decorators fire against
        # the patched settings.
        import importlib

        from app.mcp.registry import _restore_for_tests, _snapshot_for_tests
        from app.mcp.tools import chaos as chaos_pkg

        snap = _snapshot_for_tests()
        _restore_for_tests({})
        # Force reimport of the specific tool file so its decorator runs.
        importlib.reload(chaos_pkg.kill_consumer)  # type: ignore[attr-defined]
        # Also re-import the standard tools so tools/list stays populated.
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

    def _teardown():
        _restore_for_tests(snap)

    return app, _teardown


async def _mint_token(
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


def _rpc(method: str, params: dict[str, Any] | None = None, id: str = "1") -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}


# ---------------------------------------------------------------------------
# list_active_alerts
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def mcp_client(  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
):
    """Standard mcp app (CHAOS_ENABLED=false, matches production)."""
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
        yield ac, redis_stub


async def test_list_active_alerts_returns_unresolved_only(
    mcp_client,  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
) -> None:
    ac, _ = mcp_client
    db_session.add(
        Alert(
            tenant_id=default_tenant.id,
            severity="warning",
            source="slo:completion",
            title="Active alert",
        )
    )
    db_session.add(
        Alert(
            tenant_id=default_tenant.id,
            severity="info",
            source="dlq:threshold",
            title="Resolved alert",
            resolved_at=__import__("datetime").datetime.now(
                __import__("datetime").UTC
            ),
        )
    )
    await db_session.flush()

    token = await _mint_token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    resp = await ac.post(
        "/mcp",
        json=_rpc(
            "tools/call",
            {"name": "list_active_alerts", "arguments": {}},
        ),
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()
    assert "result" in body, body
    payload = json.loads(body["result"]["content"][0]["text"])
    titles = {a["title"] for a in payload["alerts"]}
    assert "Active alert" in titles
    assert "Resolved alert" not in titles


async def test_list_active_alerts_severity_filter(
    mcp_client,  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
) -> None:
    ac, _ = mcp_client
    for sev, title in [
        ("info", "info-alert"),
        ("critical", "crit-alert"),
    ]:
        db_session.add(
            Alert(
                tenant_id=default_tenant.id,
                severity=sev,
                source="test",
                title=title,
            )
        )
    await db_session.flush()

    token = await _mint_token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    resp = await ac.post(
        "/mcp",
        json=_rpc(
            "tools/call",
            {"name": "list_active_alerts", "arguments": {"severity": "critical"}},
        ),
        headers={"Authorization": f"Bearer {token}"},
    )
    payload = json.loads(resp.json()["result"]["content"][0]["text"])
    assert {a["title"] for a in payload["alerts"]} == {"crit-alert"}


async def test_list_active_alerts_wrong_scope_forbidden(
    mcp_client,  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
) -> None:
    from app.mcp import protocol

    ac, _ = mcp_client
    # telemetry:read is not incidents:read
    token = await _mint_token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    resp = await ac.post(
        "/mcp",
        json=_rpc(
            "tools/call", {"name": "list_active_alerts", "arguments": {}}
        ),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.json()["error"]["code"] == protocol.MCP_FORBIDDEN


# ---------------------------------------------------------------------------
# kill_consumer
# ---------------------------------------------------------------------------


async def test_kill_consumer_not_registered_when_chaos_disabled(
    mcp_client,  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
) -> None:
    """The chaos triple-gate — when CHAOS_ENABLED=false, the tool is
    invisible: tools/list omits it and tools/call returns TOOL_NOT_FOUND."""
    from app.mcp import protocol

    ac, _ = mcp_client
    token = await _mint_token(
        db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
    )
    resp = await ac.post(
        "/mcp",
        json=_rpc(
            "tools/call",
            {"name": "kill_consumer", "arguments": {"consumer_group": "wd"}},
        ),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.json()["error"]["code"] == protocol.MCP_TOOL_NOT_FOUND


async def test_kill_consumer_sets_redis_key_when_chaos_enabled(
    db_session: AsyncSession, default_tenant
) -> None:
    """Full round-trip with CHAOS_ENABLED=true: verify the tool
    registers, requires chaos:invoke, and sets the expected Redis key."""
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            token = await _mint_token(
                db_session,
                default_tenant.id,
                [Scope.CHAOS_INVOKE.value],
            )
            resp = await ac.post(
                "/mcp",
                json=_rpc(
                    "tools/call",
                    {
                        "name": "kill_consumer",
                        "arguments": {
                            "consumer_group": "worker-dispatcher",
                            "ttl_seconds": 30,
                        },
                    },
                ),
                headers={"Authorization": f"Bearer {token}"},
            )
        body = resp.json()
        assert "result" in body, body
        payload = json.loads(body["result"]["content"][0]["text"])
        assert payload["accepted"] is True
        assert payload["kill_key"] == "chaos:kill:worker-dispatcher"
        assert redis_stub._store["chaos:kill:worker-dispatcher"] == "killed"
    finally:
        teardown()


async def test_kill_consumer_records_chaos_audit_stream(
    db_session: AsyncSession, default_tenant
) -> None:
    """Chaos activity lands on `chaos.tool_invoked` (not `agent.tool_invoked`)
    so operators can filter it independently — see ADR 0008."""
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            token = await _mint_token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            await ac.post(
                "/mcp",
                json=_rpc(
                    "tools/call",
                    {
                        "name": "kill_consumer",
                        "arguments": {"consumer_group": "audit-writer"},
                    },
                ),
                headers={"Authorization": f"Bearer {token}"},
            )

        from app.models.audit import AuditLog

        result = await db_session.execute(
            select(AuditLog).where(AuditLog.action == "chaos.tool_invoked")
        )
        rows = list(result.scalars().all())
        assert rows, "expected chaos.tool_invoked audit row"
        row = rows[-1]
        assert row.extra_data is not None
        assert row.extra_data["tool_name"] == "kill_consumer"
        assert row.extra_data["outcome"] == "success"
    finally:
        teardown()


async def test_kill_consumer_wrong_scope_records_chaos_denied(
    db_session: AsyncSession, default_tenant
) -> None:
    """Scope refusal on a chaos tool → `chaos.tool_denied`
    (distinct from `agent.tool_invoked`/`chaos.tool_invoked`)."""
    from app.mcp import protocol

    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            # incidents:read but NOT chaos:invoke
            token = await _mint_token(
                db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
            )
            resp = await ac.post(
                "/mcp",
                json=_rpc(
                    "tools/call",
                    {
                        "name": "kill_consumer",
                        "arguments": {"consumer_group": "worker-dispatcher"},
                    },
                ),
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.json()["error"]["code"] == protocol.MCP_FORBIDDEN

        from app.models.audit import AuditLog

        result = await db_session.execute(
            select(AuditLog).where(AuditLog.action == "chaos.tool_denied")
        )
        rows = list(result.scalars().all())
        assert rows, "expected chaos.tool_denied audit row"
        assert rows[-1].extra_data["denied_by"] == "scope_check"
    finally:
        teardown()
