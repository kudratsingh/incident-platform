"""The two `[commander: telemetry]` tools over the real MCP envelope (WO-R3-312).

The unit tier proves the service's four rules. This tier proves the parts only the
envelope can: the scope gate, the audit row's action, and that the audit stream those
calls leave does not come back to the principal that wrote it — which is the half of
ADR 0035 a service-layer test cannot see, because it is `list_audit_events` doing the
withholding.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest_asyncio
from app.core.scopes import Scope
from app.dependencies import get_db, get_redis
from app.mcp.standalone import create_mcp_app
from app.models.agent_run import AgentRun
from app.models.audit import PRINCIPAL_TYPE_SERVICE_ACCOUNT
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.services.operator_audit import (
    AGENT_RUN_REPORTED_ACTION,
    TOOL_INVOKED_ACTION,
)
from app.services.service_account import ServiceAccountService
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

#: The agent's scope set after this order — reads, Tier-1 actions, and the new write.
AGENT_SCOPES = [
    Scope.TELEMETRY_READ.value,
    Scope.INCIDENTS_READ.value,
    Scope.ACTIONS_EXECUTE.value,
    Scope.AGENT_RUNS_WRITE.value,
]

#: A principal that can read but cannot report — the scope gate's negative case.
READ_ONLY_SCOPES = [Scope.TELEMETRY_READ.value, Scope.INCIDENTS_READ.value]


class _RedisStub:
    def __init__(self) -> None:
        self._store: dict[str, bytes | str] = {}

    async def get(self, key: str) -> bytes | str | None:
        return self._store.get(key)

    async def set(self, key: str, value: bytes | str, ex: int | None = None) -> bool:
        self._store[key] = value
        return True

    async def mget(self, keys: list[str]) -> list[bytes | str | None]:
        return [self._store.get(k) for k in keys]

    async def keys(self, pattern: str) -> list[str]:
        return []

    async def delete(self, *keys: str) -> int:
        return 0


@pytest_asyncio.fixture
async def mcp_client(  # type: ignore[no-untyped-def]
    db_session: AsyncSession, default_tenant
):
    app = create_mcp_app()

    async def _override_db():  # type: ignore[no-untyped-def]
        yield db_session

    async def _override_redis():  # type: ignore[no-untyped-def]
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
) -> tuple[str, uuid.UUID]:
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
        service_account=sa, scopes=None, ttl=None, minted_by_user_id=None
    )
    return plaintext, sa.id


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


def _payload(reply: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in reply, reply
    return json.loads(reply["result"]["content"][0]["text"])


async def test_a_run_report_lands_and_answers_with_a_receipt(
    mcp_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    token, sa_id = await _token(db_session, default_tenant.id, AGENT_SCOPES)
    run_id = str(uuid.uuid4())

    reply = await _call(
        mcp_client,
        token,
        "report_agent_run",
        {
            "run_id": run_id,
            "state": "investigating",
            "run_label": "consumer-outage",
            "current_hypothesis": {
                "name": "a stalled consumer",
                "category": "queue",
                "confidence": 0.7,
            },
            "last_step": {"kind": "read", "tool": "get_consumer_lag"},
        },
    )

    out = _payload(reply)
    assert out["created"] is True
    assert out["phase_appended"] is True
    assert out["phase_count"] == 1
    assert out["state"] == "investigating"
    assert out["finished_at"] is None
    assert out["accepted"] is True

    row = (
        await db_session.execute(
            select(AgentRun).where(AgentRun.id == uuid.UUID(run_id))
        )
    ).scalar_one()
    assert row.service_account_id == sa_id
    assert row.tenant_id == default_tenant.id
    assert row.scenario == "consumer-outage"


async def test_the_whole_walk_then_a_briefing(
    mcp_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    """The shape of a real run, end to end: transitions, a terminal state, one
    write-up, and a second write-up refused."""
    token, _ = await _token(db_session, default_tenant.id, AGENT_SCOPES)
    run_id = str(uuid.uuid4())
    base = datetime(2026, 9, 19, 6, 0, tzinfo=UTC)

    for offset, state in enumerate(
        ["triage", "investigating", "planning", "remediating", "verifying", "resolved"]
    ):
        out = _payload(
            await _call(
                mcp_client,
                token,
                "report_agent_run",
                {
                    "run_id": run_id,
                    "state": state,
                    "at": (base + timedelta(seconds=offset)).isoformat(),
                },
            )
        )
    assert out["phase_count"] == 6
    assert out["finished_at"] is not None

    briefing = _payload(
        await _call(
            mcp_client,
            token,
            "report_agent_briefing",
            {
                "run_id": run_id,
                "briefing": {
                    "final_state": "resolved",
                    "incident_id": "INC-1",
                    "attempted_action": "restart_consumer_group",
                },
                "prose": "Restarted the group; lag drained inside a minute.",
            },
        )
    )
    assert briefing["state"] == "resolved"
    assert briefing["accepted"] is True

    second = await _call(
        mcp_client,
        token,
        "report_agent_briefing",
        {"run_id": run_id, "briefing": {"final_state": "rewritten"}},
    )
    assert second["error"]["data"]["error_code"] == (
        "agent_run_briefing_already_recorded"
    )


async def test_a_report_after_the_terminal_state_is_refused_over_the_wire(
    mcp_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    token, _ = await _token(db_session, default_tenant.id, AGENT_SCOPES)
    run_id = str(uuid.uuid4())
    _payload(
        await _call(
            mcp_client, token, "report_agent_run", {"run_id": run_id, "state": "escalated"}
        )
    )

    reply = await _call(
        mcp_client, token, "report_agent_run", {"run_id": run_id, "state": "planning"}
    )

    assert reply["error"]["data"]["error_code"] == "agent_run_already_finished"


async def test_a_briefing_before_any_state_report_is_refused(
    mcp_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    token, _ = await _token(db_session, default_tenant.id, AGENT_SCOPES)

    reply = await _call(
        mcp_client,
        token,
        "report_agent_briefing",
        {"run_id": str(uuid.uuid4()), "briefing": {"final_state": "resolved"}},
    )

    assert reply["error"]["data"]["error_code"] == "agent_run_not_found"


async def test_a_token_without_the_scope_cannot_report(
    mcp_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    token, _ = await _token(db_session, default_tenant.id, READ_ONLY_SCOPES)

    reply = await _call(
        mcp_client,
        token,
        "report_agent_run",
        {"run_id": str(uuid.uuid4()), "state": "triage"},
    )

    assert "agent_runs:write" in reply["error"]["message"]


async def test_an_unknown_state_is_refused_before_anything_is_written(
    mcp_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    """The state vocabulary is closed on the wire, so a typo is a refusal rather than a
    row whose state no console knows how to draw."""
    token, _ = await _token(db_session, default_tenant.id, AGENT_SCOPES)
    run_id = str(uuid.uuid4())

    reply = await _call(
        mcp_client, token, "report_agent_run", {"run_id": run_id, "state": "thinking"}
    )

    assert reply["error"]["message"] == "invalid tool arguments"
    rows = (
        (await db_session.execute(select(AgentRun).where(AgentRun.id == uuid.UUID(run_id))))
        .scalars()
        .all()
    )
    assert rows == []


# --------------------------------------------------------------------------
# The audit stream: its own action, and withheld from the writer
# --------------------------------------------------------------------------


async def test_the_call_audits_as_a_run_report_not_as_an_action(
    mcp_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    """One row per call, in its own stream. An operator timeline that filed a status
    report under `agent.tool_invoked` would colour a report as something the responder
    did to the platform."""
    token, _ = await _token(db_session, default_tenant.id, AGENT_SCOPES)
    run_id = str(uuid.uuid4())

    _payload(
        await _call(
            mcp_client,
            token,
            "report_agent_run",
            {"run_id": run_id, "state": "remediating"},
        )
    )

    rows, total = await AuditRepository(db_session).list_logs(
        action=AGENT_RUN_REPORTED_ACTION, tenant_id=default_tenant.id, limit=50
    )
    assert total == 1
    row = rows[0]
    assert row.principal_type == PRINCIPAL_TYPE_SERVICE_ACCOUNT
    assert row.extra_data is not None
    assert row.extra_data["tool_name"] == "report_agent_run"
    assert row.extra_data["outcome"] == "success"
    assert row.extra_data["scope_used"] == "agent_runs:write"
    # The phase strip is rebuildable from this row alone, which is why the run id and
    # the state ride in `arguments`.
    assert row.extra_data["arguments"]["run_id"] == run_id
    assert row.extra_data["arguments"]["state"] == "remediating"

    # And it is NOT in the action stream.
    _, actions = await AuditRepository(db_session).list_logs(
        action=TOOL_INVOKED_ACTION, tenant_id=default_tenant.id, limit=50
    )
    assert actions == 0


async def test_a_refused_report_is_audited_too(
    mcp_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    """A 409 is a fact about the run's history, so it is on the record with its
    outcome. The envelope writes it, which is also what makes an unauditable report
    roll back (R2-51)."""
    token, _ = await _token(db_session, default_tenant.id, AGENT_SCOPES)
    run_id = str(uuid.uuid4())
    _payload(
        await _call(
            mcp_client, token, "report_agent_run", {"run_id": run_id, "state": "failed"}
        )
    )
    await _call(
        mcp_client, token, "report_agent_run", {"run_id": run_id, "state": "planning"}
    )

    rows, total = await AuditRepository(db_session).list_logs(
        action=AGENT_RUN_REPORTED_ACTION, tenant_id=default_tenant.id, limit=50
    )
    outcomes = sorted(r.extra_data["outcome"] for r in rows if r.extra_data)
    assert total == 2
    assert outcomes == ["error", "success"]


async def test_the_reporter_cannot_read_its_own_reports_back(
    mcp_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    """THE assertion ADR 0035's title is about, on the surface that would otherwise
    leak it: `list_audit_events` withholds `agent.run_reported` from a principal
    holding `agent_runs:write`, in SQL and out of `total`."""
    token, _ = await _token(db_session, default_tenant.id, AGENT_SCOPES)
    run_id = str(uuid.uuid4())
    _payload(
        await _call(
            mcp_client, token, "report_agent_run", {"run_id": run_id, "state": "verifying"}
        )
    )

    # Ask for the whole agent stream…
    everything = _payload(
        await _call(mcp_client, token, "list_audit_events", {"action_prefix": "agent."})
    )
    # …and for the report stream by its exact action.
    targeted = _payload(
        await _call(
            mcp_client,
            token,
            "list_audit_events",
            {"action": AGENT_RUN_REPORTED_ACTION},
        )
    )

    assert [e for e in everything["events"] if e["action"] == AGENT_RUN_REPORTED_ACTION] == []
    assert everything["total"] == 0
    # An empty page, never an error: a refusal would confirm what is being withheld.
    assert targeted["events"] == []
    assert targeted["total"] == 0


async def test_a_principal_without_the_write_scope_does_see_the_reports(
    mcp_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    """The anti-vacuity half: the rows exist and are readable — they are withheld from
    the writer, not from everyone, so the test above is proving a filter rather than an
    empty table."""
    writer, _ = await _token(db_session, default_tenant.id, AGENT_SCOPES)
    reader, _ = await _token(db_session, default_tenant.id, READ_ONLY_SCOPES)
    _payload(
        await _call(
            mcp_client,
            writer,
            "report_agent_run",
            {"run_id": str(uuid.uuid4()), "state": "triage"},
        )
    )

    seen = _payload(
        await _call(mcp_client, reader, "list_audit_events", {"action_prefix": "agent."})
    )

    assert seen["total"] >= 1
    assert any(e["action"] == AGENT_RUN_REPORTED_ACTION for e in seen["events"])


async def test_the_reporting_tools_are_absent_from_nothing_the_agent_can_read(
    mcp_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    """`tools/list` still advertises them — ADR 0016 defers principal-scoped listing —
    and that is what the `[commander:` prefix is for: the caller's planner filters on
    it, exactly as it filters `[chaos:`."""
    token, _ = await _token(db_session, default_tenant.id, AGENT_SCOPES)

    resp = await mcp_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": "1", "method": "tools/list", "params": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    tools = resp.json()["result"]["tools"]

    reported = {t["name"]: t for t in tools}
    for name in ("report_agent_run", "report_agent_briefing"):
        assert reported[name]["description"].startswith("[commander: telemetry] ")
        assert reported[name]["required_scope"] == "agent_runs:write"
        assert reported[name]["is_idempotent"] is False


# --------------------------------------------------------------------------
# The run record over the envelope (WO-R3-328, ADR 0037)
# --------------------------------------------------------------------------


async def test_one_report_carries_the_whole_reasoning_and_answers_with_the_ledger(
    mcp_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    """The shape the reporter actually sends on a transition into `remediating`, over the
    real envelope — and the receipt that lets it log the ledger's size without a read."""
    token, _ = await _token(db_session, default_tenant.id, AGENT_SCOPES)
    run_id = str(uuid.uuid4())

    out = _payload(
        await _call(
            mcp_client,
            token,
            "report_agent_run",
            {
                "run_id": run_id,
                "state": "remediating",
                "hypotheses": [
                    {
                        "name": "a stalled consumer",
                        "category": "queue",
                        "confidence": 0.82,
                        "reasoning_excerpt": "one group climbing, the others flat",
                    },
                    {"name": "a slow downstream", "confidence": 0.1},
                ],
                "plan": {
                    "action_tool": "restart_consumer_group",
                    "action_arguments": {"consumer_group": "worker-dispatcher"},
                    "target_hypothesis": "a stalled consumer",
                    "rationale_excerpt": "cheapest test of the leading explanation",
                },
                "step": {
                    "seq": 7,
                    "kind": "action",
                    "tool": "restart_consumer_group",
                    "arguments": {"consumer_group": "worker-dispatcher"},
                    "result_excerpt": '{"accepted": true, "kill_key_cleared": true}',
                    "outcome": "success",
                    "latency_ms": 41.0,
                },
                "budget": {"tool_calls_used": 7, "tool_calls_max": 13, "usd_used": 0.4},
            },
        )
    )

    assert out["steps_count"] == 1
    assert out["steps_dropped"] == 0
    assert out["accepted"] is True

    row = (
        await db_session.execute(
            select(AgentRun).where(AgentRun.id == uuid.UUID(run_id))
        )
    ).scalar_one()
    assert [h["name"] for h in row.hypotheses] == [
        "a stalled consumer",
        "a slow downstream",
    ]
    assert row.plan["action_arguments"] == {"consumer_group": "worker-dispatcher"}
    assert row.steps[0]["seq"] == 7
    assert row.budget["tool_calls_max"] == 13


async def test_a_verify_poll_lands_twice_and_a_step_only_report_keeps_the_rest(
    mcp_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    token, _ = await _token(db_session, default_tenant.id, AGENT_SCOPES)
    run_id = str(uuid.uuid4())
    await _call(
        mcp_client,
        token,
        "report_agent_run",
        {
            "run_id": run_id,
            "state": "verifying",
            "hypotheses": [{"name": "a stalled consumer", "confidence": 0.9}],
        },
    )

    for attempt, verdict in enumerate(["not_verified", "verified"], start=1):
        _payload(
            await _call(
                mcp_client,
                token,
                "report_agent_run",
                {
                    "run_id": run_id,
                    "state": "verifying",
                    "verification": {
                        "verdict": verdict,
                        "attempt": attempt,
                        "of": 3,
                        "reasoning_excerpt": "lag reading after the restart",
                    },
                },
            )
        )
    out = _payload(
        await _call(
            mcp_client,
            token,
            "report_agent_run",
            {
                "run_id": run_id,
                "state": "verifying",
                "step": {"seq": 1, "kind": "read", "tool": "get_consumer_lag"},
            },
        )
    )

    assert out["steps_count"] == 1
    row = (
        await db_session.execute(
            select(AgentRun).where(AgentRun.id == uuid.UUID(run_id))
        )
    ).scalar_one()
    assert [v["verdict"] for v in row.verifications] == ["not_verified", "verified"]
    assert row.verification["verdict"] == "verified"
    # The step-only report left both the ranking and the verdicts alone.
    assert [h["name"] for h in row.hypotheses] == ["a stalled consumer"]


async def test_an_over_long_excerpt_is_refused_at_the_envelope(
    mcp_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    """The limit is a refusal on the wire, not a truncation in the service — so the
    caller finds out, and no row is written."""
    token, _ = await _token(db_session, default_tenant.id, AGENT_SCOPES)
    run_id = str(uuid.uuid4())

    reply = await _call(
        mcp_client,
        token,
        "report_agent_run",
        {
            "run_id": run_id,
            "state": "investigating",
            "step": {"seq": 1, "kind": "read", "result_excerpt": "x" * 401},
        },
    )

    assert "error" in reply, reply
    rows = (
        (
            await db_session.execute(
                select(AgentRun).where(AgentRun.id == uuid.UUID(run_id))
            )
        )
        .scalars()
        .all()
    )
    assert rows == []
