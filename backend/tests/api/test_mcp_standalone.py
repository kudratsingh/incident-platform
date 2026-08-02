"""End-to-end tests for the standalone MCP process — full JSON-RPC
round-trip against the real dispatch layer.

Covers PR-3's test bar:
  - initialize handshake works unauthenticated
  - tools/list unauthenticated → 401 (via MCP_UNAUTHORIZED)
  - tools/call unauthenticated → 401
  - tools/call wrong scope → 403 (via MCP_FORBIDDEN)
  - tools/call happy path returns real value + writes an audit row
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
from app.models.audit import PRINCIPAL_TYPE_SERVICE_ACCOUNT, AuditLog
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.services.service_account import ServiceAccountService
from app.utils.backpressure import BACKPRESSURE_LAG_KEY
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class _RedisStub:
    """Minimal Redis stub — only implements the ops the MCP surface hits.
    Keeps tests deterministic without spinning up real Redis."""

    def __init__(self, values: dict[str, bytes | str] | None = None) -> None:
        self._store: dict[str, bytes | str] = dict(values or {})

    async def get(self, key: str) -> bytes | str | None:
        return self._store.get(key)


@pytest_asyncio.fixture
async def mcp_client(  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
):
    """Yields (client, redis_stub). Same pattern for every test —
    stateful cache injection is done by mutating `redis_stub._store`."""

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


async def _mint_token(
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    scopes: list[str],
) -> str:
    """Create an SA + mint a token, returning the plaintext bearer."""
    svc = ServiceAccountService(
        ServiceAccountRepository(db_session),
        ServiceAccountTokenRepository(db_session),
        AuditRepository(db_session),
    )
    sa = await svc.create_service_account(
        tenant_id=default_tenant.id,
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
# initialize
# ---------------------------------------------------------------------------


async def test_initialize_works_without_auth(mcp_client) -> None:  # type: ignore[no-untyped-def]
    ac, _ = mcp_client
    resp = await ac.post("/mcp", json=_rpc("initialize", {}))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "1"
    assert "result" in body
    assert body["result"]["serverInfo"]["name"] == "incident-platform-mcp"
    assert body["result"]["protocolVersion"]


async def test_initialize_accepts_client_info(mcp_client) -> None:  # type: ignore[no-untyped-def]
    ac, _ = mcp_client
    resp = await ac.post(
        "/mcp",
        json=_rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "clientInfo": {"name": "incident-commander", "version": "0.1.0"},
            },
        ),
    )
    assert resp.status_code == 200
    assert "result" in resp.json()


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------


async def test_tools_list_without_auth_returns_unauthorized(mcp_client) -> None:  # type: ignore[no-untyped-def]
    ac, _ = mcp_client
    resp = await ac.post("/mcp", json=_rpc("tools/list"))
    body = resp.json()
    assert body["error"]["code"] == protocol.MCP_UNAUTHORIZED


async def test_tools_list_with_auth_returns_registered_tools(
    mcp_client,  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
) -> None:
    ac, _ = mcp_client
    token = await _mint_token(
        db_session, default_tenant, [Scope.TELEMETRY_READ.value]
    )
    resp = await ac.post(
        "/mcp",
        json=_rpc("tools/list"),
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()
    assert "result" in body
    names = {t["name"] for t in body["result"]["tools"]}
    assert "get_consumer_lag" in names


async def test_tools_list_includes_output_schema_per_tool(
    mcp_client,  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
) -> None:
    """v0.4.8 extension (FIX_PLAN #25 optional): every tool advertises
    its outputSchema alongside inputSchema so the commander's
    contract-snapshot job can diff live-platform-advertised output
    shapes without relying on its local registry as the source of
    truth. Extension field — MCP-compliant clients that don't know
    about it will ignore it."""
    ac, _ = mcp_client
    token = await _mint_token(
        db_session, default_tenant, [Scope.TELEMETRY_READ.value]
    )
    resp = await ac.post(
        "/mcp",
        json=_rpc("tools/list"),
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()
    assert "result" in body
    tools = body["result"]["tools"]
    assert tools, "expected at least one tool registered"

    # Every advertised tool must include outputSchema as a JSON Schema
    # object. Empty {} is not acceptable — every tool has a declared
    # output model, so the schema must be populated.
    for tool in tools:
        assert "outputSchema" in tool, f"{tool['name']} missing outputSchema"
        assert isinstance(tool["outputSchema"], dict), tool["name"]
        assert tool["outputSchema"].get("type") == "object", (
            f"{tool['name']} outputSchema not a JSON object schema"
        )

    # Spot-check: get_consumer_lag advertises the same shape its output
    # model declares — lag: int | None + consumer_group + cache_key.
    lag_tool = next(t for t in tools if t["name"] == "get_consumer_lag")
    props = lag_tool["outputSchema"]["properties"]
    assert "consumer_group" in props
    assert "lag" in props
    assert "cache_key" in props


# ---------------------------------------------------------------------------
# tools/call
# ---------------------------------------------------------------------------


async def test_tools_call_without_auth_unauthorized(mcp_client) -> None:  # type: ignore[no-untyped-def]
    ac, _ = mcp_client
    resp = await ac.post(
        "/mcp",
        json=_rpc("tools/call", {"name": "get_consumer_lag", "arguments": {}}),
    )
    body = resp.json()
    assert body["error"]["code"] == protocol.MCP_UNAUTHORIZED


async def test_tools_call_wrong_scope_forbidden(
    mcp_client,  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
) -> None:
    # SA has incidents:read only — get_consumer_lag requires telemetry:read
    ac, _ = mcp_client
    token = await _mint_token(
        db_session, default_tenant, [Scope.INCIDENTS_READ.value]
    )
    resp = await ac.post(
        "/mcp",
        json=_rpc("tools/call", {"name": "get_consumer_lag", "arguments": {}}),
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()
    assert body["error"]["code"] == protocol.MCP_FORBIDDEN


async def test_tools_call_unknown_tool_returns_tool_not_found(
    mcp_client,  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
) -> None:
    ac, _ = mcp_client
    token = await _mint_token(
        db_session, default_tenant, [Scope.TELEMETRY_READ.value]
    )
    resp = await ac.post(
        "/mcp",
        json=_rpc("tools/call", {"name": "not_a_real_tool", "arguments": {}}),
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()
    assert body["error"]["code"] == protocol.MCP_TOOL_NOT_FOUND


async def test_tools_call_happy_path_returns_cached_lag(
    mcp_client,  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
) -> None:
    ac, redis_stub = mcp_client
    redis_stub._store[BACKPRESSURE_LAG_KEY] = "42"
    token = await _mint_token(
        db_session, default_tenant, [Scope.TELEMETRY_READ.value]
    )
    resp = await ac.post(
        "/mcp",
        json=_rpc("tools/call", {"name": "get_consumer_lag", "arguments": {}}),
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()
    assert "result" in body, body
    content = body["result"]["content"]
    assert len(content) == 1
    payload = json.loads(content[0]["text"])
    assert payload["consumer_group"] == "worker-dispatcher"
    assert payload["lag"] == 42


async def test_tools_call_returns_null_lag_when_cache_empty(
    mcp_client,  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
) -> None:
    ac, _ = mcp_client
    token = await _mint_token(
        db_session, default_tenant, [Scope.TELEMETRY_READ.value]
    )
    resp = await ac.post(
        "/mcp",
        json=_rpc("tools/call", {"name": "get_consumer_lag", "arguments": {}}),
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()
    assert "result" in body
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["lag"] is None


async def test_tools_call_writes_audit_row(
    mcp_client,  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
) -> None:
    ac, redis_stub = mcp_client
    redis_stub._store[BACKPRESSURE_LAG_KEY] = "7"
    token = await _mint_token(
        db_session, default_tenant, [Scope.TELEMETRY_READ.value]
    )
    resp = await ac.post(
        "/mcp",
        json=_rpc("tools/call", {"name": "get_consumer_lag", "arguments": {}}),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    result = await db_session.execute(
        select(AuditLog).where(AuditLog.action == "agent.tool_invoked")
    )
    rows = list(result.scalars().all())
    assert rows, "expected an agent.tool_invoked audit row"
    row = rows[-1]
    assert row.principal_type == PRINCIPAL_TYPE_SERVICE_ACCOUNT
    assert row.extra_data is not None
    assert row.extra_data["tool_name"] == "get_consumer_lag"
    assert row.extra_data["outcome"] == "success"
    assert row.extra_data["scope_used"] == "telemetry:read"
