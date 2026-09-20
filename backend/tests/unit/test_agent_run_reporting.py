"""The four rules of `agent_runs`, and the one rule about who may read it back.

WO-R3-312 / ADR 0035. The write side is easy to get wrong in ways a console makes
visible: a phase history that grows once per call instead of once per transition, a
terminal state that a stale report rewinds, a briefing that a retry overwrites while an
operator is reading it. Each of those is one test here.

The last group is the half the ADR's title is about: the principal that writes this
stream cannot read it. There is no read tool for `agent_runs`, and the audit rows those
writes leave are withheld from the writer — otherwise `list_audit_events` would be a
read surface for `agent_runs` under another name.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import app.mcp.tools  # noqa: F401  — import fires every @tool decorator
import pytest
from app.core.scopes import ALL_SCOPES, API_GRANTABLE_SCOPES, Scope
from app.dependencies import Principal
from app.mcp.commander import COMMANDER_DESCRIPTION_PREFIX, is_commander_tool
from app.mcp.registry import ToolContext, list_tools
from app.models.agent_run import AgentRun
from app.models.enums import TERMINAL_AGENT_RUN_STATES, AgentRunState
from app.models.service_account import ServiceAccount
from app.repositories.agent_run import AgentRunRepository
from app.services.agent_run import (
    AgentRunAlreadyFinishedError,
    AgentRunBriefingAlreadyRecordedError,
    AgentRunNotFoundError,
    AgentRunService,
)
from app.services.operator_audit import (
    AGENT_RUN_REPORTED_ACTION,
    CHAOS_ACTION_PREFIX,
    LAB_ACTION_PREFIX,
    TOOL_INVOKED_ACTION,
    hidden_audit_action_prefixes,
)
from sqlalchemy.ext.asyncio import AsyncSession

_T0 = datetime(2026, 9, 19, 5, 0, 0, tzinfo=UTC)


async def _service_account(session: AsyncSession, tenant_id: uuid.UUID) -> ServiceAccount:
    sa = ServiceAccount(
        tenant_id=tenant_id,
        name=f"reporter-{uuid.uuid4().hex[:8]}",
        scopes=[Scope.AGENT_RUNS_WRITE.value],
        is_active=True,
    )
    session.add(sa)
    await session.flush()
    return sa


@pytest.fixture
def svc(db_session: AsyncSession) -> AgentRunService:
    return AgentRunService(AgentRunRepository(db_session))


# --------------------------------------------------------------------------
# Rule 1 — upsert by run id, and a repeat is a no-op
# --------------------------------------------------------------------------


async def test_the_first_report_creates_the_run_with_one_phase_entry(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    sa = await _service_account(db_session, default_tenant.id)
    run_id = uuid.uuid4()

    outcome = await svc.report_run(
        run_id=run_id,
        tenant_id=default_tenant.id,
        service_account_id=sa.id,
        state=AgentRunState.TRIAGE.value,
        at=_T0,
        scenario="consumer-outage",
    )

    assert outcome.created is True
    assert outcome.phase_appended is True
    assert outcome.run.state == "triage"
    assert outcome.run.scenario == "consumer-outage"
    assert [e["state"] for e in outcome.run.phase_history] == ["triage"]
    assert outcome.run.finished_at is None


async def test_the_same_state_reported_twice_changes_nothing(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    """THE idempotency assertion. A caller that retries a timed-out report, or reports
    on every loop iteration, must not turn a timeline into a call log."""
    sa = await _service_account(db_session, default_tenant.id)
    run_id = uuid.uuid4()
    for _ in range(3):
        outcome = await svc.report_run(
            run_id=run_id,
            tenant_id=default_tenant.id,
            service_account_id=sa.id,
            state=AgentRunState.INVESTIGATING.value,
            at=_T0,
        )

    assert outcome.created is False
    assert outcome.phase_appended is False
    assert len(outcome.run.phase_history) == 1


# --------------------------------------------------------------------------
# Rule 2 — phase history is append-only, one entry per transition
# --------------------------------------------------------------------------


async def test_phase_history_appends_once_per_transition_and_keeps_order(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    sa = await _service_account(db_session, default_tenant.id)
    run_id = uuid.uuid4()
    walk = [
        AgentRunState.TRIAGE,
        AgentRunState.INVESTIGATING,
        AgentRunState.INVESTIGATING,  # a repeat, mid-walk
        AgentRunState.PLANNING,
        AgentRunState.REMEDIATING,
        AgentRunState.VERIFYING,
        AgentRunState.RESOLVED,
    ]
    for offset, state in enumerate(walk):
        outcome = await svc.report_run(
            run_id=run_id,
            tenant_id=default_tenant.id,
            service_account_id=sa.id,
            state=state.value,
            at=_T0 + timedelta(seconds=offset),
        )

    assert [e["state"] for e in outcome.run.phase_history] == [
        "triage",
        "investigating",
        "planning",
        "remediating",
        "verifying",
        "resolved",
    ]
    # Oldest first, and every entry carries its own time.
    times = [e["at"] for e in outcome.run.phase_history]
    assert times == sorted(times)


async def test_a_revisited_state_is_appended_again_not_deduplicated(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    """A responder may go back to investigating after a failed plan. That is a real
    transition and the timeline must show both visits."""
    sa = await _service_account(db_session, default_tenant.id)
    run_id = uuid.uuid4()
    for offset, state in enumerate(
        [
            AgentRunState.INVESTIGATING,
            AgentRunState.PLANNING,
            AgentRunState.INVESTIGATING,
        ]
    ):
        outcome = await svc.report_run(
            run_id=run_id,
            tenant_id=default_tenant.id,
            service_account_id=sa.id,
            state=state.value,
            at=_T0 + timedelta(seconds=offset),
        )

    assert [e["state"] for e in outcome.run.phase_history] == [
        "investigating",
        "planning",
        "investigating",
    ]


async def test_hypothesis_and_last_step_are_replaced_while_history_is_not(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    """The two current-reading fields replace; the history does not. Omitting a reading
    clears it, which is how a responder says "I no longer have one"."""
    sa = await _service_account(db_session, default_tenant.id)
    run_id = uuid.uuid4()
    await svc.report_run(
        run_id=run_id,
        tenant_id=default_tenant.id,
        service_account_id=sa.id,
        state=AgentRunState.INVESTIGATING.value,
        at=_T0,
        current_hypothesis={"name": "saturation", "category": "queue", "confidence": 0.6},
        last_step={"kind": "read", "tool": "get_consumer_lag", "at": None},
    )
    outcome = await svc.report_run(
        run_id=run_id,
        tenant_id=default_tenant.id,
        service_account_id=sa.id,
        state=AgentRunState.PLANNING.value,
        at=_T0 + timedelta(seconds=5),
        current_hypothesis=None,
        last_step=None,
    )

    assert outcome.run.current_hypothesis is None
    assert outcome.run.last_step is None
    assert len(outcome.run.phase_history) == 2


async def test_alert_id_and_label_are_filled_in_but_never_cleared(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    sa = await _service_account(db_session, default_tenant.id)
    run_id = uuid.uuid4()
    alert_id = uuid.uuid4()
    await svc.report_run(
        run_id=run_id,
        tenant_id=default_tenant.id,
        service_account_id=sa.id,
        state=AgentRunState.TRIAGE.value,
        at=_T0,
    )
    await svc.report_run(
        run_id=run_id,
        tenant_id=default_tenant.id,
        service_account_id=sa.id,
        state=AgentRunState.INVESTIGATING.value,
        at=_T0 + timedelta(seconds=1),
        alert_id=alert_id,
        scenario="dlq-backlog",
    )
    outcome = await svc.report_run(
        run_id=run_id,
        tenant_id=default_tenant.id,
        service_account_id=sa.id,
        state=AgentRunState.PLANNING.value,
        at=_T0 + timedelta(seconds=2),
    )

    assert outcome.run.alert_id == alert_id
    assert outcome.run.scenario == "dlq-backlog"


# --------------------------------------------------------------------------
# Rule 3 — a terminal state closes the run
# --------------------------------------------------------------------------


@pytest.mark.parametrize("state", sorted(TERMINAL_AGENT_RUN_STATES))
async def test_every_terminal_state_stamps_finished_at(
    db_session: AsyncSession, default_tenant, svc: AgentRunService, state: str
) -> None:
    sa = await _service_account(db_session, default_tenant.id)
    outcome = await svc.report_run(
        run_id=uuid.uuid4(),
        tenant_id=default_tenant.id,
        service_account_id=sa.id,
        state=state,
        at=_T0,
    )

    assert outcome.run.finished_at is not None


@pytest.mark.parametrize(
    "state",
    sorted(set(AgentRunState) - TERMINAL_AGENT_RUN_STATES),
)
async def test_no_other_state_stamps_finished_at(
    db_session: AsyncSession, default_tenant, svc: AgentRunService, state: str
) -> None:
    sa = await _service_account(db_session, default_tenant.id)
    outcome = await svc.report_run(
        run_id=uuid.uuid4(),
        tenant_id=default_tenant.id,
        service_account_id=sa.id,
        state=state,
        at=_T0,
    )

    assert outcome.run.finished_at is None


async def test_a_report_on_a_closed_run_is_refused(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    """A late or duplicated report must not rewind a strip an operator has already
    read to its ending."""
    sa = await _service_account(db_session, default_tenant.id)
    run_id = uuid.uuid4()
    await svc.report_run(
        run_id=run_id,
        tenant_id=default_tenant.id,
        service_account_id=sa.id,
        state=AgentRunState.ESCALATED.value,
        at=_T0,
    )

    with pytest.raises(AgentRunAlreadyFinishedError) as excinfo:
        await svc.report_run(
            run_id=run_id,
            tenant_id=default_tenant.id,
            service_account_id=sa.id,
            state=AgentRunState.INVESTIGATING.value,
            at=_T0 + timedelta(seconds=1),
        )

    assert excinfo.value.status_code == 409
    assert excinfo.value.error_code == "agent_run_already_finished"


# --------------------------------------------------------------------------
# Rule 4 — the briefing lands once
# --------------------------------------------------------------------------


async def test_the_briefing_is_stored_with_its_prose_inside_it(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    sa = await _service_account(db_session, default_tenant.id)
    run_id = uuid.uuid4()
    await svc.report_run(
        run_id=run_id,
        tenant_id=default_tenant.id,
        service_account_id=sa.id,
        state=AgentRunState.ESCALATED.value,
        at=_T0,
    )

    run = await svc.report_briefing(
        run_id=run_id,
        tenant_id=default_tenant.id,
        briefing={"final_state": "escalated", "escalation_reason": "budget spent"},
        prose="Lag never drained.",
        at=_T0 + timedelta(seconds=1),
    )

    assert run.briefing is not None
    assert run.briefing["final_state"] == "escalated"
    assert run.briefing["prose"] == "Lag never drained."
    assert run.briefing["recorded_at"].startswith("2026-09-19T05:00:01")


async def test_a_second_briefing_is_refused(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    """THE assertion the 409 exists for: an operator reading a write-up must not have
    it change underneath them."""
    sa = await _service_account(db_session, default_tenant.id)
    run_id = uuid.uuid4()
    await svc.report_run(
        run_id=run_id,
        tenant_id=default_tenant.id,
        service_account_id=sa.id,
        state=AgentRunState.RESOLVED.value,
        at=_T0,
    )
    await svc.report_briefing(
        run_id=run_id,
        tenant_id=default_tenant.id,
        briefing={"final_state": "resolved"},
        prose=None,
        at=_T0,
    )

    with pytest.raises(AgentRunBriefingAlreadyRecordedError) as excinfo:
        await svc.report_briefing(
            run_id=run_id,
            tenant_id=default_tenant.id,
            briefing={"final_state": "something else"},
            prose="a rewrite",
            at=_T0,
        )

    assert excinfo.value.status_code == 409
    assert excinfo.value.error_code == "agent_run_briefing_already_recorded"
    run = await AgentRunRepository(db_session).get_for_tenant(run_id, default_tenant.id)
    assert run is not None
    assert run.briefing is not None
    assert run.briefing["final_state"] == "resolved"


async def test_a_briefing_for_an_unknown_run_is_refused(
    default_tenant, svc: AgentRunService
) -> None:
    with pytest.raises(AgentRunNotFoundError) as excinfo:
        await svc.report_briefing(
            run_id=uuid.uuid4(),
            tenant_id=default_tenant.id,
            briefing={"final_state": "resolved"},
            prose=None,
            at=_T0,
        )

    assert excinfo.value.status_code == 409
    assert excinfo.value.error_code == "agent_run_not_found"


async def test_a_briefing_closes_a_run_whose_last_state_was_not_terminal(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    """A write-up arrives at the end, so it closes the run even if the caller never
    reported a terminal state — better a closed run than one open forever."""
    sa = await _service_account(db_session, default_tenant.id)
    run_id = uuid.uuid4()
    await svc.report_run(
        run_id=run_id,
        tenant_id=default_tenant.id,
        service_account_id=sa.id,
        state=AgentRunState.VERIFYING.value,
        at=_T0,
    )

    run = await svc.report_briefing(
        run_id=run_id,
        tenant_id=default_tenant.id,
        briefing={"final_state": "unknown"},
        prose=None,
        at=_T0 + timedelta(seconds=2),
    )

    assert run.finished_at is not None
    # And the state is untouched: the platform does not infer one from a write-up.
    assert run.state == "verifying"


# --------------------------------------------------------------------------
# Tenant scoping in the app layer (RLS is the backstop, proved on Postgres)
# --------------------------------------------------------------------------


async def test_a_run_is_invisible_to_another_tenant(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    from app.models.tenant import Tenant

    other = Tenant(
        id=uuid.uuid4(), slug=f"other-{uuid.uuid4().hex[:6]}", name="Other", is_active=True
    )
    db_session.add(other)
    await db_session.flush()
    sa = await _service_account(db_session, default_tenant.id)
    run_id = uuid.uuid4()
    await svc.report_run(
        run_id=run_id,
        tenant_id=default_tenant.id,
        service_account_id=sa.id,
        state=AgentRunState.TRIAGE.value,
        at=_T0,
    )

    repo = AgentRunRepository(db_session)
    assert await repo.get_for_tenant(run_id, default_tenant.id) is not None
    assert await repo.get_for_tenant(run_id, other.id) is None
    rows, total = await repo.list_for_tenant(other.id)
    assert (rows, total) == ([], 0)


async def test_the_active_filter_is_the_absence_of_finished_at(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    sa = await _service_account(db_session, default_tenant.id)
    open_id, closed_id = uuid.uuid4(), uuid.uuid4()
    await svc.report_run(
        run_id=open_id,
        tenant_id=default_tenant.id,
        service_account_id=sa.id,
        state=AgentRunState.INVESTIGATING.value,
        at=_T0,
    )
    await svc.report_run(
        run_id=closed_id,
        tenant_id=default_tenant.id,
        service_account_id=sa.id,
        state=AgentRunState.RESOLVED.value,
        at=_T0,
    )

    repo = AgentRunRepository(db_session)
    active, active_total = await repo.list_for_tenant(default_tenant.id, active=True)
    done, done_total = await repo.list_for_tenant(default_tenant.id, active=False)
    both, both_total = await repo.list_for_tenant(default_tenant.id)

    assert [r.id for r in active] == [open_id]
    assert [r.id for r in done] == [closed_id]
    assert (active_total, done_total, both_total) == (1, 1, 2)
    assert len(both) == 2


# --------------------------------------------------------------------------
# The `[commander:` family, and the census it stays out of
# --------------------------------------------------------------------------


def test_both_tools_carry_the_commander_prefix_and_the_flag() -> None:
    """Two ways to say one thing, so a tool cannot carry the label without the
    behaviour or the behaviour without the label."""
    commander = [t for t in list_tools() if t.is_commander]

    assert sorted(t.name for t in commander) == [
        "report_agent_briefing",
        "report_agent_run",
    ]
    for td in commander:
        assert td.description.startswith(COMMANDER_DESCRIPTION_PREFIX)
        assert is_commander_tool(td) is True
        assert td.required_scope is Scope.AGENT_RUNS_WRITE
        assert td.is_chaos is False
        # Not idempotency-key guarded: the upsert makes a repeat a no-op on its own,
        # so a key would add a failure mode without adding a guarantee.
        assert td.is_idempotent is False


def test_no_other_tool_carries_the_commander_prefix() -> None:
    """The prefix is what the caller's planner filters on, so a tool wearing it
    without the flag would vanish from the planner for the wrong reason."""
    for td in list_tools():
        if td.description.startswith(COMMANDER_DESCRIPTION_PREFIX):
            assert td.is_commander is True, td.name


def test_the_commander_tools_are_not_in_the_read_census() -> None:
    """ADR 0035's shape: these are writes, and no read scope reaches them. A console
    reads `agent_runs` over REST; nothing on the agent's surface does."""
    read_scopes = {Scope.TELEMETRY_READ, Scope.INCIDENTS_READ}
    for td in list_tools():
        if td.is_commander:
            assert td.required_scope not in read_scopes, td.name


def test_no_tool_reads_agent_runs_at_all() -> None:
    """The strongest form of the rule, read off the registry: `agent_runs:write` is
    held by exactly the two writers, and nothing else mentions the table."""
    by_scope = [t for t in list_tools() if t.required_scope is Scope.AGENT_RUNS_WRITE]

    assert sorted(t.name for t in by_scope) == [
        "report_agent_briefing",
        "report_agent_run",
    ]
    # A read tool would have to return the row; no output model on the agent's surface
    # carries the table's own fields.
    for td in list_tools():
        if td.is_commander:
            continue
        fields = set(td.output_model.model_fields)
        assert "phase_history" not in fields, td.name
        assert "briefing" not in fields, td.name


def test_the_new_scope_is_known_and_grantable_through_the_admin_api() -> None:
    """Unlike `chaos:invoke` this one is not the lab: an operator provisioning a
    responder may grant it, and the seed script grants it by default."""
    assert Scope.AGENT_RUNS_WRITE.value == "agent_runs:write"
    assert Scope.AGENT_RUNS_WRITE.value in ALL_SCOPES
    assert Scope.AGENT_RUNS_WRITE.value in API_GRANTABLE_SCOPES


# --------------------------------------------------------------------------
# The writer of the stream is not its reader
# --------------------------------------------------------------------------


def _principal(*scopes: Scope) -> Principal:
    return Principal(
        kind="service_account",
        tenant_id=uuid.uuid4(),
        service_account=ServiceAccount(
            id=uuid.uuid4(), tenant_id=uuid.uuid4(), name="p", scopes=[]
        ),
        scopes=frozenset(s.value for s in scopes),
    )


def test_the_run_report_stream_is_hidden_from_the_principal_that_writes_it() -> None:
    """THE assertion ADR 0035's title is about. Without it, `list_audit_events` is a
    read surface for `agent_runs` under another name."""
    writer = _principal(Scope.TELEMETRY_READ, Scope.AGENT_RUNS_WRITE)

    hidden = hidden_audit_action_prefixes(writer)

    assert AGENT_RUN_REPORTED_ACTION in hidden


def test_a_principal_without_the_write_scope_still_sees_the_run_reports() -> None:
    """The evaluator and a human reading REST see everything. The rule is about the
    writer, not about the stream being secret."""
    reader = _principal(Scope.TELEMETRY_READ, Scope.INCIDENTS_READ)

    hidden = hidden_audit_action_prefixes(reader)

    assert AGENT_RUN_REPORTED_ACTION not in hidden
    # And the pre-existing rule is untouched: no chaos scope, no lab streams. Both
    # prefixes since WO-R3-327 — `chaos.` is the fault, `lab.` is the world reset, one
    # condition (ADR 0012's 2026-09-20 amendment).
    assert hidden == (CHAOS_ACTION_PREFIX, LAB_ACTION_PREFIX)


def test_the_chaos_rule_is_unchanged_for_the_evaluator() -> None:
    evaluator = _principal(
        Scope.TELEMETRY_READ, Scope.INCIDENTS_READ, Scope.CHAOS_INVOKE
    )

    assert hidden_audit_action_prefixes(evaluator) == ()


def test_the_run_report_action_is_not_the_action_stream() -> None:
    """A status report is not something the responder did to the platform, and an
    operator timeline that mixed them would colour one as the other."""
    assert AGENT_RUN_REPORTED_ACTION == "agent.run_reported"
    assert AGENT_RUN_REPORTED_ACTION != TOOL_INVOKED_ACTION
    assert AGENT_RUN_REPORTED_ACTION.startswith("agent.")
    # And it is not in the chaos stream, so the chaos withholding does not touch it.
    assert not AGENT_RUN_REPORTED_ACTION.startswith(CHAOS_ACTION_PREFIX)


# --------------------------------------------------------------------------
# Anti-vacuity
# --------------------------------------------------------------------------


def test_the_state_enum_is_the_responders_own_nine_names() -> None:
    """Character for character the commander's `IncidentState` values, so its reporter
    maps nothing (coordinator correction, 2026-09-19: the brief's draft said `triaging`
    and left out `awaiting_approval` — both wrong). A member added on one side and not
    the other is the drift this pin exists to catch."""
    assert [s.value for s in AgentRunState] == [
        "triage",
        "investigating",
        "planning",
        "awaiting_approval",
        "remediating",
        "verifying",
        "resolved",
        "escalated",
        "failed",
    ]
    assert TERMINAL_AGENT_RUN_STATES == {"resolved", "escalated", "failed"}
    # `awaiting_approval` is emphatically not terminal: a run parked on an approval is
    # still open, and closing it would make the console stop watching.
    assert AgentRunState.AWAITING_APPROVAL.value not in TERMINAL_AGENT_RUN_STATES


def test_the_model_carries_every_column_the_order_names() -> None:
    assert set(AgentRun.__table__.columns.keys()) == {
        "id",
        "tenant_id",
        "alert_id",
        "service_account_id",
        "scenario",
        "state",
        "phase_history",
        "current_hypothesis",
        "last_step",
        "briefing",
        "started_at",
        "updated_at",
        "finished_at",
        # WO-R3-328's seven (ADR 0037) — the reasoning beside the state. Their own
        # rules are tested in `test_agent_run_record.py`.
        "hypotheses",
        "plan",
        "verification",
        "verifications",
        "steps",
        "steps_dropped",
        "budget",
    }


def test_the_tool_context_type_is_the_one_the_handlers_build() -> None:
    """Anti-vacuity guard for the API tier below: the handlers take a `ToolContext`,
    so a signature drift there would fail loudly rather than skip these tests."""
    assert ToolContext.__dataclass_fields__.keys() == {"db", "redis", "principal"}
