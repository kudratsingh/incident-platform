"""The `chaos.` audit stream is readable only by a principal that may fire it."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
import pytest_asyncio
from app.core.scopes import Scope
from app.dependencies import get_db, get_redis
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
from app.services.operator_audit import (
    CHAOS_ACTION_PREFIX,
    CHAOS_TOOL_DENIED_ACTION,
    CHAOS_TOOL_INVOKED_ACTION,
)
from app.services.service_account import ServiceAccountService
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

# The two principals the token split creates. Spelled out rather than
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

_TRACE_ID = "11111111-2222-3333-4444-555555555555"


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


async def _call(
    ac: AsyncClient, token: str, tool: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    resp = await ac.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp.json()


def _content(body: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in body, body
    return json.loads(body["result"]["content"][0]["text"])


async def _seed_chaos_world(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """Three visible rows and three the lab wrote."""
    rows = [
        AuditLog(
            tenant_id=tenant_id,
            action="job.created",
            principal_type=PRINCIPAL_TYPE_USER,
            principal_id=uuid.uuid4(),
            request_id=_TRACE_ID,
        ),
        AuditLog(
            tenant_id=tenant_id,
            action="agent.tool_invoked",
            principal_type=PRINCIPAL_TYPE_SERVICE_ACCOUNT,
            principal_id=uuid.uuid4(),
            resource_type="mcp_tool",
            resource_id="list_dlq_messages",
            request_id=_TRACE_ID,
            extra_data={"tool_name": "list_dlq_messages", "outcome": "success"},
        ),
        AuditLog(
            tenant_id=tenant_id,
            action="service_account.created",
            principal_type=PRINCIPAL_TYPE_USER,
            principal_id=uuid.uuid4(),
        ),
        AuditLog(
            tenant_id=tenant_id,
            action=CHAOS_TOOL_INVOKED_ACTION,
            principal_type=PRINCIPAL_TYPE_SERVICE_ACCOUNT,
            principal_id=uuid.uuid4(),
            resource_type="mcp_tool",
            resource_id="kill_consumer",
            request_id=_TRACE_ID,
            extra_data={
                "tool_name": "kill_consumer",
                "arguments": {"consumer_group": "worker-dispatcher"},
                "outcome": "success",
            },
        ),
        AuditLog(
            tenant_id=tenant_id,
            action=CHAOS_TOOL_INVOKED_ACTION,
            principal_type=PRINCIPAL_TYPE_SERVICE_ACCOUNT,
            principal_id=uuid.uuid4(),
            resource_type="mcp_tool",
            resource_id="seed_dlq_messages",
            request_id=_TRACE_ID,
            extra_data={
                "tool_name": "seed_dlq_messages",
                "arguments": {"count": 4},
                "outcome": "success",
            },
        ),
        AuditLog(
            tenant_id=tenant_id,
            action=CHAOS_TOOL_DENIED_ACTION,
            principal_type=PRINCIPAL_TYPE_SERVICE_ACCOUNT,
            principal_id=uuid.uuid4(),
            resource_type="mcp_tool",
            resource_id="bad_deploy",
            extra_data={
                "tool_name": "bad_deploy",
                "arguments": {},
                "denied_by": "scope_check",
                "outcome": "unauthorized",
            },
        ),
    ]
    for row in rows:
        db_session.add(row)
    await db_session.flush()


_VISIBLE_ACTIONS = {
    "job.created",
    "agent.tool_invoked",
    "service_account.created",
}
_CHAOS_ACTIONS = {CHAOS_TOOL_INVOKED_ACTION, CHAOS_TOOL_DENIED_ACTION}


def _chaos_rows(payload: dict[str, Any]) -> int:
    return sum(
        1
        for e in payload["events"]
        if e["action"].startswith(CHAOS_ACTION_PREFIX)
    )


def _assert_total_matches_the_page(payload: dict[str, Any]) -> None:
    """`total` and the page must agree when nothing was capped."""
    assert payload["total"] == len(payload["events"]), (
        f"total {payload['total']} over a {len(payload['events'])}-row page "
        "reports rows the caller may not read"
    )


# ---------------------------------------------------------------------------


async def test_chaos_principal_sees_the_chaos_stream(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    """The half that must keep working: the evaluator reads everything."""
    await _seed_chaos_world(db_session, default_tenant.id)
    token = await _token(db_session, default_tenant.id, EVALUATOR_SCOPES)

    payload = _content(await _call(mcp_client, token, "list_audit_events", {}))
    actions = {e["action"] for e in payload["events"]}
    assert _CHAOS_ACTIONS <= actions
    assert _VISIBLE_ACTIONS <= actions
    assert _chaos_rows(payload) == 3


async def test_agent_principal_sees_no_chaos_rows(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    """THE assertion."""
    await _seed_chaos_world(db_session, default_tenant.id)
    token = await _token(db_session, default_tenant.id, AGENT_SCOPES)

    payload = _content(await _call(mcp_client, token, "list_audit_events", {}))
    actions = {e["action"] for e in payload["events"]}
    assert _VISIBLE_ACTIONS <= actions
    assert _chaos_rows(payload) == 0
    _assert_total_matches_the_page(payload)
    assert "chaos" not in json.dumps(payload)


async def test_agent_principal_prefix_filter_returns_an_empty_page(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    """`action_prefix='chaos.'` is answered, not refused."""
    await _seed_chaos_world(db_session, default_tenant.id)
    token = await _token(db_session, default_tenant.id, AGENT_SCOPES)

    body = await _call(
        mcp_client,
        token,
        "list_audit_events",
        {"action_prefix": CHAOS_ACTION_PREFIX},
    )
    assert "error" not in body, "a withheld stream is an empty page, not a refusal"
    payload = _content(body)
    assert payload == {"total": 0, "events": []}


@pytest.mark.parametrize(
    "action", sorted(_CHAOS_ACTIONS)
)
async def test_agent_principal_exact_action_returns_an_empty_page(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    action: str,
) -> None:
    """The withholding is on the rows, not on the prefix filter."""
    await _seed_chaos_world(db_session, default_tenant.id)
    token = await _token(db_session, default_tenant.id, AGENT_SCOPES)

    body = await _call(mcp_client, token, "list_audit_events", {"action": action})
    assert "error" not in body
    assert _content(body) == {"total": 0, "events": []}


async def test_agent_principal_prefix_filter_on_a_visible_stream_still_works(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    """The exclusion AND-s with the caller's filter rather than replacing it — `agent.`
    must still isolate the agent's own stream, which is the crash-reconciliation use
    case the tool exists for."""
    await _seed_chaos_world(db_session, default_tenant.id)
    token = await _token(db_session, default_tenant.id, AGENT_SCOPES)

    payload = _content(
        await _call(
            mcp_client, token, "list_audit_events", {"action_prefix": "agent."}
        )
    )
    assert {e["action"] for e in payload["events"]} == {"agent.tool_invoked"}
    assert payload["total"] == 1


async def test_agent_principal_type_filter_does_not_reopen_the_stream(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    """Every chaos row is a service-account row, so this filter is the natural way to ask
    for machine activity — and it must not become a way around the withholding."""
    await _seed_chaos_world(db_session, default_tenant.id)
    token = await _token(db_session, default_tenant.id, AGENT_SCOPES)

    payload = _content(
        await _call(
            mcp_client,
            token,
            "list_audit_events",
            {"principal_type": "service_account"},
        )
    )
    assert {e["action"] for e in payload["events"]} == {"agent.tool_invoked"}
    assert payload["total"] == 1


# ---------------------------------------------------------------------------


async def test_get_trace_withholds_chaos_rows_and_their_count(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    await _seed_chaos_world(db_session, default_tenant.id)
    token = await _token(db_session, default_tenant.id, AGENT_SCOPES)

    payload = _content(
        await _call(mcp_client, token, "get_trace", {"trace_id": _TRACE_ID})
    )
    actions = {e["action"] for e in payload["audit_events"]}
    assert actions == {"job.created", "agent.tool_invoked"}
    assert payload["total_audit_events"] == 2, (
        "the count has to drop with the rows, or `truncated` becomes a "
        "statement about hidden rows"
    )
    assert payload["truncated"] is False
    assert "chaos" not in json.dumps(payload)


async def test_get_trace_shows_chaos_rows_to_the_evaluator(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    await _seed_chaos_world(db_session, default_tenant.id)
    token = await _token(db_session, default_tenant.id, EVALUATOR_SCOPES)

    payload = _content(
        await _call(mcp_client, token, "get_trace", {"trace_id": _TRACE_ID})
    )
    actions = {e["action"] for e in payload["audit_events"]}
    assert CHAOS_TOOL_INVOKED_ACTION in actions
    assert payload["total_audit_events"] == 4
