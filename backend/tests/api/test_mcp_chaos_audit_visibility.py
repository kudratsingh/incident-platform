"""The `chaos.` and `lab.` audit streams are readable only by a principal that may fire the lab.

`lab.world_reset` joined the withheld set in WO-R3-327 (ADR 0012's 2026-09-20 amendment). It
carries a sharper leak than a hook name: its payload is the reset's own counters, which name
every mechanism the lab swept.

`lab.probe` joined it in WO-R3-333 (ADR 0038), and it arrives by a different route: not a row
some script appended, but the label a real `tools/call` carries when the lab made it **under
the agent's own token**. The second half of this file is that end to end — who may apply the
label, what the agent gets when it tries, and that the row lands on the operator's side of the
withholding and not the agent's.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from app.config import Settings
from app.core.scopes import Scope
from app.dependencies import get_db, get_redis
from app.mcp.lab_probe import LAB_PRINCIPAL_HEADER, LAB_PROBE_REASON_MAX_LENGTH
from app.mcp.protocol import LAB_PROBE_FIELD
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
    LAB_ACTION_PREFIX,
    LAB_PROBE_ACTION,
    TOOL_INVOKED_ACTION,
    WORLD_RESET_ACTION,
    WORLD_RESET_RESOURCE_TYPE,
)
from app.services.service_account import ServiceAccountService
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
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
# The third: read-only, and holding the agent's scopes exactly, which is why the rule has to
# know it by name (ADR 0038).
SMOKE_SCOPES = [Scope.TELEMETRY_READ.value, Scope.INCIDENTS_READ.value]
SMOKE_ACCOUNT_NAME = "incident-commander-smoke"

_TRACE_ID = "11111111-2222-3333-4444-555555555555"


@contextmanager
def lab_enabled(smoke_name: str = SMOKE_ACCOUNT_NAME):  # type: ignore[no-untyped-def]
    """A stack with a lab on it. The label is gated on `CHAOS_ENABLED` (ADR 0008), so
    every honoured-probe test says so out loud; the refusal without it has its own test."""
    with patch(
        "app.mcp.lab_probe.get_settings",
        return_value=Settings(
            chaos_enabled=True,
            environment="test",
            lab_probe_smoke_account_name=smoke_name,
        ),
    ):
        yield


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
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
    scopes: list[str],
    *,
    name: str | None = None,
) -> str:
    svc = ServiceAccountService(
        ServiceAccountRepository(db_session),
        ServiceAccountTokenRepository(db_session),
        AuditRepository(db_session),
    )
    sa = await svc.create_service_account(
        tenant_id=tenant_id,
        name=name or f"probe-{uuid.uuid4().hex[:8]}",
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
    ac: AsyncClient,
    token: str,
    tool: str,
    arguments: dict[str, Any],
    *,
    lab_probe: str | None = None,
    lab_credential: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"name": tool, "arguments": arguments}
    if lab_probe is not None:
        # Beside `arguments`, never inside it — that placement is the contract.
        params[LAB_PROBE_FIELD] = lab_probe
    headers = {"Authorization": f"Bearer {token}"}
    if lab_credential is not None:
        headers[LAB_PRINCIPAL_HEADER] = f"Bearer {lab_credential}"
    resp = await ac.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": "1", "method": "tools/call", "params": params},
        headers=headers,
    )
    return resp.json()


async def _rows(db_session: AsyncSession, action: str) -> list[AuditLog]:
    result = await db_session.execute(
        select(AuditLog).where(AuditLog.action == action)
    )
    return list(result.scalars().all())


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
        # The boundary row the environment reset appends (WO-R3-327). Its payload names
        # the apparatus, which is why it is withheld beside the rows above.
        AuditLog(
            tenant_id=tenant_id,
            action=WORLD_RESET_ACTION,
            principal_type=PRINCIPAL_TYPE_SERVICE_ACCOUNT,
            principal_id=uuid.uuid4(),
            resource_type=WORLD_RESET_RESOURCE_TYPE,
            extra_data={
                "chaos_keys_cleared": 4,
                "seeded_dlq_deleted": 5,
                "hot_set_reseeded": 1,
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


def _lab_rows(payload: dict[str, Any]) -> int:
    return sum(
        1 for e in payload["events"] if e["action"].startswith(LAB_ACTION_PREFIX)
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
    # And the boundary: whoever may fire the lab may read when the lab last reset it.
    assert _lab_rows(payload) == 1


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
    assert _lab_rows(payload) == 0
    _assert_total_matches_the_page(payload)
    assert "chaos" not in json.dumps(payload)
    # The boundary's payload is the mechanism list, so its counter names must not
    # survive either — a leak by field name rather than by action name.
    body = json.dumps(payload)
    for counter in ("seeded_dlq_deleted", "hot_set_reseeded"):
        assert counter not in body


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


async def test_agent_principal_lab_prefix_filter_returns_an_empty_page(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    """`action_prefix='lab.'` is answered the same way — asking by name must not be the
    one call that confirms the stream exists."""
    await _seed_chaos_world(db_session, default_tenant.id)
    token = await _token(db_session, default_tenant.id, AGENT_SCOPES)

    body = await _call(
        mcp_client,
        token,
        "list_audit_events",
        {"action_prefix": LAB_ACTION_PREFIX},
    )
    assert "error" not in body, "a withheld stream is an empty page, not a refusal"
    assert _content(body) == {"total": 0, "events": []}


@pytest.mark.parametrize(
    "action", sorted(_CHAOS_ACTIONS | {WORLD_RESET_ACTION})
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


# ---------------------------------------------------------------------------
# `lab.probe` — the label, the credential it needs, and who reads the row
# (WO-R3-333, ADR 0038)
# ---------------------------------------------------------------------------

_PROBED_TOOL = "get_consumer_lag"
_PROBED_ARGS = {"consumer_group": "worker-dispatcher"}
_REASON = "principal guard: the agent token must be refused here"


async def test_an_agent_token_alone_cannot_relabel_its_own_read(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    """THE assertion on this side. If the field were honoured on the agent's token alone,
    the agent could lift its own reads out of the ledger by adding one key — the audit
    trail would be something the subject under test writes."""
    agent = await _token(db_session, default_tenant.id, AGENT_SCOPES)

    with lab_enabled():
        body = await _call(
            mcp_client, agent, _PROBED_TOOL, _PROBED_ARGS, lab_probe=_REASON
        )

    assert body["error"]["code"] == -32602, body
    assert body["error"]["data"]["error_code"] == "lab_probe_refused"
    assert body["error"]["data"]["reason_code"] == "credential_missing"
    assert LAB_PRINCIPAL_HEADER in body["error"]["message"]

    # Refused, never ignored: no labelled row, and the call itself did not run.
    assert await _rows(db_session, LAB_PROBE_ACTION) == []
    refused = await _rows(db_session, TOOL_INVOKED_ACTION)
    assert len(refused) == 1
    assert refused[0].extra_data["outcome"] == "error"
    assert LAB_PROBE_FIELD in refused[0].extra_data["error_message"]


async def test_the_agents_own_credential_in_the_header_buys_nothing(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    """The obvious next try: present the same token twice."""
    agent = await _token(
        db_session, default_tenant.id, AGENT_SCOPES, name="incident-commander"
    )

    with lab_enabled():
        body = await _call(
            mcp_client,
            agent,
            _PROBED_TOOL,
            _PROBED_ARGS,
            lab_probe=_REASON,
            lab_credential=agent,
        )

    assert body["error"]["data"]["reason_code"] == "credential_not_authorised"
    assert await _rows(db_session, LAB_PROBE_ACTION) == []


async def test_the_evaluator_credential_labels_a_call_made_on_the_agents_token(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    """The shape the principal guards need: the call is the agent's, because what that
    token can do is the thing being proved, and the row says the lab made it."""
    agent = await _token(db_session, default_tenant.id, AGENT_SCOPES)
    evaluator = await _token(
        db_session,
        default_tenant.id,
        EVALUATOR_SCOPES,
        name="incident-commander-chaos",
    )

    with lab_enabled():
        body = await _call(
            mcp_client,
            agent,
            _PROBED_TOOL,
            _PROBED_ARGS,
            lab_probe=_REASON,
            lab_credential=evaluator,
        )

    assert "error" not in body, body
    labelled = await _rows(db_session, LAB_PROBE_ACTION)
    assert len(labelled) == 1
    row = labelled[0]
    assert row.extra_data["tool_name"] == _PROBED_TOOL
    assert row.extra_data["lab_probe_reason"] == _REASON
    assert row.extra_data["lab_probe_principal"] == "incident-commander-chaos"
    # The one thing the label must not do: leave a second row in the agent's stream.
    assert await _rows(db_session, TOOL_INVOKED_ACTION) == []


async def test_the_read_only_smoke_credential_labels_a_call(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    """The world audit's credential — it holds no write scope at all, so it is matched by
    name and the read-only claim is re-checked."""
    agent = await _token(db_session, default_tenant.id, AGENT_SCOPES)
    smoke = await _token(
        db_session, default_tenant.id, SMOKE_SCOPES, name=SMOKE_ACCOUNT_NAME
    )

    with lab_enabled():
        body = await _call(
            mcp_client,
            agent,
            _PROBED_TOOL,
            _PROBED_ARGS,
            lab_probe="world audit read",
            lab_credential=smoke,
        )

    assert "error" not in body, body
    labelled = await _rows(db_session, LAB_PROBE_ACTION)
    assert len(labelled) == 1
    assert labelled[0].extra_data["lab_probe_principal"] == SMOKE_ACCOUNT_NAME


async def test_the_field_inside_arguments_is_refused_by_the_tool_and_labels_nothing(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    """The placement is the contract. Inside `arguments` the field is an argument: the
    tool's own `extra="forbid"` refuses it, and the row stays the agent's."""
    agent = await _token(db_session, default_tenant.id, AGENT_SCOPES)
    evaluator = await _token(db_session, default_tenant.id, EVALUATOR_SCOPES)

    with lab_enabled():
        body = await _call(
            mcp_client,
            agent,
            _PROBED_TOOL,
            {**_PROBED_ARGS, LAB_PROBE_FIELD: _REASON},
            lab_credential=evaluator,
        )

    assert body["error"]["code"] == -32602, body
    assert body["error"]["message"] == "invalid tool arguments"
    assert await _rows(db_session, LAB_PROBE_ACTION) == []
    assert len(await _rows(db_session, TOOL_INVOKED_ACTION)) == 1


async def test_an_over_long_reason_is_refused_with_a_valid_credential(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    agent = await _token(db_session, default_tenant.id, AGENT_SCOPES)
    evaluator = await _token(db_session, default_tenant.id, EVALUATOR_SCOPES)

    with lab_enabled():
        body = await _call(
            mcp_client,
            agent,
            _PROBED_TOOL,
            _PROBED_ARGS,
            lab_probe="x" * (LAB_PROBE_REASON_MAX_LENGTH + 1),
            lab_credential=evaluator,
        )

    assert body["error"]["data"]["reason_code"] == "reason_invalid"
    assert await _rows(db_session, LAB_PROBE_ACTION) == []


async def test_a_stack_with_no_lab_on_it_refuses_the_label(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    """No `lab_enabled()` here, so `chaos_enabled` is off — a production deployment has no
    lab and therefore no way to relabel an audit row, whatever credential arrives."""
    agent = await _token(db_session, default_tenant.id, AGENT_SCOPES)
    evaluator = await _token(db_session, default_tenant.id, EVALUATOR_SCOPES)

    body = await _call(
        mcp_client,
        agent,
        _PROBED_TOOL,
        _PROBED_ARGS,
        lab_probe=_REASON,
        lab_credential=evaluator,
    )

    assert body["error"]["data"]["reason_code"] == "not_available"
    assert await _rows(db_session, LAB_PROBE_ACTION) == []


async def test_a_labelled_row_is_withheld_from_the_agent_and_read_by_the_evaluator(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    """F4 closed at the read surface too. A read the agent did not make must not come back
    to it as its own — otherwise the label only moves the confusion one layer down."""
    agent = await _token(db_session, default_tenant.id, AGENT_SCOPES)
    evaluator = await _token(db_session, default_tenant.id, EVALUATOR_SCOPES)

    with lab_enabled():
        assert "error" not in await _call(
            mcp_client,
            agent,
            _PROBED_TOOL,
            _PROBED_ARGS,
            lab_probe=_REASON,
            lab_credential=evaluator,
        )

    as_agent = _content(await _call(mcp_client, agent, "list_audit_events", {}))
    assert _lab_rows(as_agent) == 0
    _assert_total_matches_the_page(as_agent)
    body = json.dumps(as_agent)
    assert LAB_PROBE_ACTION not in body
    assert _REASON not in body

    as_evaluator = _content(await _call(mcp_client, evaluator, "list_audit_events", {}))
    assert LAB_PROBE_ACTION in {e["action"] for e in as_evaluator["events"]}


async def test_the_agent_asking_for_the_probe_stream_by_name_gets_an_empty_page(
    mcp_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    """Withholding is not refusing: an error would confirm the stream exists, which is
    the fact being withheld."""
    agent = await _token(db_session, default_tenant.id, AGENT_SCOPES)
    evaluator = await _token(db_session, default_tenant.id, EVALUATOR_SCOPES)

    with lab_enabled():
        await _call(
            mcp_client,
            agent,
            _PROBED_TOOL,
            _PROBED_ARGS,
            lab_probe=_REASON,
            lab_credential=evaluator,
        )

    body = await _call(
        mcp_client, agent, "list_audit_events", {"action": LAB_PROBE_ACTION}
    )
    assert "error" not in body
    assert _content(body) == {"total": 0, "events": []}
