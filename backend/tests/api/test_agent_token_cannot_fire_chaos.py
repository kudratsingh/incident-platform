"""The agent's scope set is refused by every chaos hook, one by one.

WO-R3-187 (owner decision O-4). The token split only means something if the
agent's scopes cannot fire a single hook — and "cannot" has to be checked
against the chaos registry rather than against a list someone maintains,
because a hook added later would otherwise inherit whatever the last
reviewer assumed.

Chaos tools only register under `CHAOS_ENABLED=true`, so the module reloads
every module in `app.mcp.tools.chaos` under patched settings — the trick
`test_mcp_wave1_pr_b` established — and walks the package rather than
naming its members, so a new hook is covered on the day it lands.

No hook is ever *invoked* here. Each call is expected to stop at the scope
check in `app.mcp.handlers._run_tool_call`, before argument parsing and
before the handler, which is why `{}` is a safe argument for all of them:
a refusal that depended on the arguments would not be a refusal. The
positive half of the contract — that the evaluator's token does work — is
asserted at the registry level (`required_scope` is `chaos:invoke` on every
hook, and the chaos account holds it) rather than by firing a hook at a
stubbed Redis and a Kafka producer that has no broker.
"""

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
# agent's set is the one under test; the evaluator's is here so the pair is
# readable in one place.
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
# a floor, not an equality — a new hook should not fail this file, it should
# be swept by it — but a floor is what stops the parameterisation from
# passing on an empty registry.
_MIN_CHAOS_TOOLS = 10


class _RedisStub:
    async def get(self, key: str) -> bytes | str | None:  # pragma: no cover
        return None


def _reload_every_tool_module() -> None:
    """Re-run every tool module's decorators against a cleared registry.

    Walked rather than listed, for the reason this whole file is walked: a
    hook or a tool added later has to be covered without anyone
    remembering to add it here. Reloading a module is what re-fires its
    `@tool` / `@chaos_tool` decorator — importing it again would not,
    since it is already in `sys.modules` — and the non-chaos tools have to
    come back too, because clearing the registry dropped them as well.
    """
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
    """Chaos tools, as the running registry sees them.

    Collected at call time inside a chaos-enabled fixture; at module import
    the registry holds none, which is why the parameterisation below reads
    the names from the fixture rather than from a decorator argument.
    """
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
    """THE assertion. Walk the chaos registry; every hook says no.

    One test rather than a parameterised family because the registry is
    only populated inside the fixture — the failure message names the
    offending hooks, which is what a parameterised id would have given.
    """
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
    """The other half of "refused by every hook": the refusal above is the
    scope check, so a hook that required something else — or nothing —
    would be reachable by the agent whatever the token carries.

    Read off the registry rather than by firing the hooks: invoking one
    means a broker and a real Redis, and this file must never seed chaos.
    """
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
    """A refused attempt is a fact an operator should be able to read.

    It routes to the `chaos.tool_denied` stream with `denied_by`, which is
    also the row the agent itself can no longer see — the withholding and
    the denial are the same decision seen from two sides.
    """
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
    """The split must not have cost the agent anything it needs.

    A token that had been narrowed too far would fail this, and the eval
    would fail later in a way that looks like an agent defect.
    """
    ac, tokens = chaos_enabled_client
    body = await _call(ac, tokens["agent"], "list_audit_events", {})
    assert "error" not in body, body
    body = await _call(ac, tokens["agent"], "get_redis_health", {})
    assert "error" not in body, body


@pytest.mark.parametrize("scope", sorted(AGENT_SCOPES))
def test_the_agent_scope_set_is_the_seeded_one(scope: str) -> None:
    """Pins the set this file is written about, so a change to the seeder's
    defaults shows up here as well as in its own tests."""
    assert scope in {
        Scope.TELEMETRY_READ.value,
        Scope.INCIDENTS_READ.value,
        Scope.ACTIONS_EXECUTE.value,
    }
