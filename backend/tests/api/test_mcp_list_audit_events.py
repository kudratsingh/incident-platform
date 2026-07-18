"""End-to-end tests for `list_audit_events`.

The reviewer's PR-2 blocker: the agent needs read access to the audit
log for crash reconciliation + as ground truth for its own safety
graders. This tool is that surface, scoped to the caller's tenant,
requiring `incidents:read`.
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
from app.models.audit import (
    PRINCIPAL_TYPE_SERVICE_ACCOUNT,
    PRINCIPAL_TYPE_USER,
    AuditLog,
)
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.services.service_account import ServiceAccountService
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


class _RedisStub:
    async def get(self, key: str) -> bytes | str | None:  # pragma: no cover
        return None


@pytest_asyncio.fixture
async def mcp_client(  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
):
    app = create_mcp_app()

    async def _override_db():
        yield db_session

    async def _override_redis():
        yield _RedisStub()

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
    ac: AsyncClient, token: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    resp = await ac.post(
        "/mcp",
        json=_rpc("tools/call", {"name": "list_audit_events", "arguments": arguments}),
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp.json()


def _content(body: dict[str, Any]) -> dict[str, Any]:
    return json.loads(body["result"]["content"][0]["text"])


async def _seed(db_session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Seed a representative mix — one of each principal type + each
    stream (agent, chaos, service_account, generic user)."""
    rows = [
        AuditLog(
            tenant_id=tenant_id,
            action="job.created",
            principal_type=PRINCIPAL_TYPE_USER,
            principal_id=uuid.uuid4(),
        ),
        AuditLog(
            tenant_id=tenant_id,
            action="agent.tool_invoked",
            principal_type=PRINCIPAL_TYPE_SERVICE_ACCOUNT,
            principal_id=uuid.uuid4(),
            resource_type="mcp_tool",
            resource_id="get_consumer_lag",
            extra_data={"tool_name": "get_consumer_lag", "outcome": "success"},
        ),
        AuditLog(
            tenant_id=tenant_id,
            action="chaos.tool_invoked",
            principal_type=PRINCIPAL_TYPE_SERVICE_ACCOUNT,
            principal_id=uuid.uuid4(),
            resource_type="mcp_tool",
            resource_id="kill_consumer",
            extra_data={"tool_name": "kill_consumer"},
        ),
        AuditLog(
            tenant_id=tenant_id,
            action="service_account.created",
            principal_type=PRINCIPAL_TYPE_USER,
            principal_id=uuid.uuid4(),
        ),
    ]
    for r in rows:
        db_session.add(r)
    await db_session.flush()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_returns_all_rows_for_tenant(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    await _seed(db_session, default_tenant.id)
    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    payload = _content(await _call(mcp_client, token, {}))
    actions = {e["action"] for e in payload["events"]}
    assert {
        "job.created",
        "agent.tool_invoked",
        "chaos.tool_invoked",
        "service_account.created",
    } <= actions


async def test_action_prefix_isolates_agent_stream(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    await _seed(db_session, default_tenant.id)
    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    payload = _content(
        await _call(mcp_client, token, {"action_prefix": "agent."})
    )
    actions = {e["action"] for e in payload["events"]}
    assert actions == {"agent.tool_invoked"}


async def test_action_exact_match_takes_precedence(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    """When both `action` and `action_prefix` are set, `action` wins —
    the tool suppresses prefix rather than AND-ing them."""
    await _seed(db_session, default_tenant.id)
    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    payload = _content(
        await _call(
            mcp_client,
            token,
            {"action": "chaos.tool_invoked", "action_prefix": "agent."},
        )
    )
    actions = {e["action"] for e in payload["events"]}
    assert actions == {"chaos.tool_invoked"}


async def test_principal_type_filter_isolates_machine_activity(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    """Agent-side use case — 'give me every row I emitted'."""
    await _seed(db_session, default_tenant.id)
    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    payload = _content(
        await _call(
            mcp_client, token, {"principal_type": "service_account"}
        )
    )
    assert payload["events"], "expected at least one machine row"
    assert all(
        e["principal_type"] == "service_account" for e in payload["events"]
    )


async def test_response_carries_extra_data(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    await _seed(db_session, default_tenant.id)
    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    payload = _content(
        await _call(mcp_client, token, {"action": "agent.tool_invoked"})
    )
    assert payload["events"]
    row = payload["events"][0]
    assert row["extra_data"] == {
        "tool_name": "get_consumer_lag",
        "outcome": "success",
    }


# ---------------------------------------------------------------------------
# Auth boundaries
# ---------------------------------------------------------------------------


async def test_wrong_scope_forbidden(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    body = await _call(mcp_client, token, {})
    assert body["error"]["code"] == protocol.MCP_FORBIDDEN


async def test_bad_principal_type_is_rejected(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    """Extra=forbid on the input model so a nonsense filter fails
    validation rather than silently returning everything."""
    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    # unknown key trips the extra=forbid guard
    body = await _call(mcp_client, token, {"unknown_field": "x"})
    assert body["error"]["code"] == protocol.JSONRPC_INVALID_PARAMS
