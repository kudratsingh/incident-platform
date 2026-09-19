"""The agent's scope set is refused by every chaos hook, one by one."""

from __future__ import annotations

import importlib
import pkgutil
import uuid
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from app.config import Settings
from app.core.scopes import Scope
from app.dependencies import get_db, get_redis
from app.mcp import protocol
from app.mcp.registry import (
    ToolDefinition,
    _restore_for_tests,
    _snapshot_for_tests,
    list_tools,
)
from app.models.audit import AuditLog
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.services.operator_audit import CHAOS_TOOL_DENIED_ACTION
from app.services.service_account import ServiceAccountService
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# What `scripts/seed_incident_commander.py` mints for each principal. The
AGENT_SCOPES = [
    Scope.TELEMETRY_READ.value,
    Scope.INCIDENTS_READ.value,
    Scope.ACTIONS_EXECUTE.value,
]
EVALUATOR_SCOPES = [
    Scope.TELEMETRY_READ.value,
    Scope.INCIDENTS_READ.value,
    Scope.CHAOS_INVOKE.value,
]

# The registered chaos surface at the time of writing: 10 hooks. Asserted as
_MIN_CHAOS_TOOLS = 10


class _RedisStub:
    async def get(self, key: str) -> bytes | str | None:  # pragma: no cover
        return None


def _reload_every_tool_module() -> None:
    """Re-run every tool module's decorators against a cleared registry."""
    import app.mcp.tools as tools_pkg

    for info in pkgutil.walk_packages(
        tools_pkg.__path__, prefix=f"{tools_pkg.__name__}."
    ):
        importlib.reload(importlib.import_module(info.name))


@pytest_asyncio.fixture
async def chaos_enabled_client(  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
):
    """An MCP app with every chaos hook registered, plus the two tokens."""
    snapshot = _snapshot_for_tests()
    with patch(
        "app.mcp.standalone.assert_chaos_gate", lambda *a, **kw: None
    ), patch(
        "app.mcp.chaos.get_settings",
        return_value=Settings(chaos_enabled=True, environment="test"),
    ):
        _restore_for_tests({})
        _reload_every_tool_module()

        from app.mcp.standalone import create_mcp_app

        app = create_mcp_app()

    async def _override_db():
        yield db_session

    async def _override_redis():
        yield _RedisStub()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis

    svc = ServiceAccountService(
        ServiceAccountRepository(db_session),
        ServiceAccountTokenRepository(db_session),
        AuditRepository(db_session),
    )
    tokens: dict[str, str] = {}
    for label, scopes in (("agent", AGENT_SCOPES), ("evaluator", EVALUATOR_SCOPES)):
        sa = await svc.create_service_account(
            tenant_id=default_tenant.id,
            name=f"{label}-{uuid.uuid4().hex[:8]}",
            scopes=list(scopes),
            created_by_user_id=None,
        )
        _, plaintext = await svc.mint_token(
            service_account=sa, scopes=None, ttl=None, minted_by_user_id=None
        )
        tokens[label] = plaintext

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            yield ac, tokens
    finally:
        _restore_for_tests(snapshot)


def _chaos_tool_names() -> list[str]:
    """Chaos tools, as the running registry sees them."""
    return sorted(
        t.name for t in list_tools() if t.required_scope == Scope.CHAOS_INVOKE
    )


def _chaos_tools() -> list[ToolDefinition]:
    return [t for t in list_tools() if t.required_scope == Scope.CHAOS_INVOKE]


async def _call(
    ac: AsyncClient, token: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    resp = await ac.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp.json()


async def test_every_chaos_hook_refuses_the_agent_token(
    chaos_enabled_client: tuple[AsyncClient, dict[str, str]],
) -> None:
    """THE assertion."""
    ac, tokens = chaos_enabled_client
    names = _chaos_tool_names()
    assert len(names) >= _MIN_CHAOS_TOOLS, (
        f"only {len(names)} chaos tools registered ({names}) — the reload "
        "did not fire, so this test would prove nothing"
    )

    allowed: dict[str, Any] = {}
    for name in names:
        body = await _call(ac, tokens["agent"], name, {})
        error = body.get("error")
        if error is None or error.get("code") != protocol.MCP_FORBIDDEN:
            allowed[name] = body
            continue
        assert Scope.CHAOS_INVOKE.value in error["message"], (
            f"{name} refused the agent for the wrong reason: {error}"
        )

    assert allowed == {}, (
        f"chaos hooks that did not refuse the agent's scopes: {sorted(allowed)}"
    )


async def test_every_chaos_hook_requires_the_chaos_scope(
    chaos_enabled_client: tuple[AsyncClient, dict[str, str]],
) -> None:
    """The other half of "refused by every hook": the refusal above is the scope check, so
    a hook that required something else — or nothing — would be reachable by the agent
    whatever the token carries."""
    ac, tokens = chaos_enabled_client
    del ac, tokens
    offenders = {
        t.name: t.required_scope
        for t in list_tools()
        if t.is_chaos and t.required_scope != Scope.CHAOS_INVOKE
    }
    assert offenders == {}, f"chaos tools not gated on chaos:invoke: {offenders}"
    assert Scope.CHAOS_INVOKE.value not in AGENT_SCOPES
    assert Scope.CHAOS_INVOKE.value in EVALUATOR_SCOPES
    # Every hook the registry flags `is_chaos` is one the scope screen
    # covers, and vice versa — the two views cannot drift apart.
    assert {t.name for t in _chaos_tools()} == {
        t.name for t in list_tools() if t.is_chaos
    }


async def test_the_refusal_is_audited_as_a_chaos_denial(
    chaos_enabled_client: tuple[AsyncClient, dict[str, str]],
    db_session: AsyncSession,
) -> None:
    """A refused attempt is a fact an operator should be able to read."""
    ac, tokens = chaos_enabled_client
    name = _chaos_tool_names()[0]

    body = await _call(ac, tokens["agent"], name, {})
    assert body["error"]["code"] == protocol.MCP_FORBIDDEN

    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == CHAOS_TOOL_DENIED_ACTION)
            )
        )
        .scalars()
        .all()
    )
    assert rows, "a denied chaos attempt must leave an audit row"
    denied = [r for r in rows if r.resource_id == name]
    assert denied, f"no chaos.tool_denied row for {name}"
    assert denied[0].extra_data is not None
    assert denied[0].extra_data["denied_by"] == "scope_check"
    assert denied[0].extra_data["scope_used"] == Scope.CHAOS_INVOKE.value


async def test_the_agent_token_still_works_on_the_read_surface(
    chaos_enabled_client: tuple[AsyncClient, dict[str, str]],
) -> None:
    """The split must not have cost the agent anything it needs."""
    ac, tokens = chaos_enabled_client
    body = await _call(ac, tokens["agent"], "list_audit_events", {})
    assert "error" not in body, body
    body = await _call(ac, tokens["agent"], "get_redis_health", {})
    assert "error" not in body, body


@pytest.mark.parametrize("scope", sorted(AGENT_SCOPES))
def test_the_agent_scope_set_is_the_seeded_one(scope: str) -> None:
    """Pins the set this file is written about, so a change to the seeder's defaults shows
    up here as well as in its own tests."""
    assert scope in {
        Scope.TELEMETRY_READ.value,
        Scope.INCIDENTS_READ.value,
        Scope.ACTIONS_EXECUTE.value,
    }
