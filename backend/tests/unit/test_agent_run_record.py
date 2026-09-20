"""The two rules WO-R3-328 adds to `agent_runs`, and the wire that enforces them.

ADR 0037. WO-R3-312's four rules are tested in `test_agent_run_reporting.py` and are not
repeated here; these are the two the reasoning columns add.

**Rule 5 — a reading is filled in, never cleared.** `hypotheses`, `plan`, `verification`
and `budget` survive a report that omits them, where `current_hypothesis` and `last_step`
do not. This is the fix for the demo's first take arriving from the other direction: the
reporter now reports after every tool call, most of those reports say nothing about
hypotheses, and under replace-or-clear each one would blank the panel.

**Rule 6 — the ledgers append, bounded, and say what a bound cost.** One step per call,
`seq` is its identity, the caps drop the oldest, and `steps_dropped` counts them.

The last group is the wire: the excerpt limits refuse rather than truncate, because a
silent cut stores something the caller did not write — and because there is no value of
`result_excerpt` that should turn this table into a store of whole tool outputs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.models.enums import AgentRunState
from app.models.service_account import ServiceAccount
from app.repositories.agent_run import AgentRunRepository
from app.services.agent_run import (
    STEPS_CAP,
    VERIFICATIONS_CAP,
    AgentRunAlreadyFinishedError,
    AgentRunService,
)
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

_T0 = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


async def _service_account(session: AsyncSession, tenant_id: uuid.UUID) -> ServiceAccount:
    sa = ServiceAccount(
        tenant_id=tenant_id,
        name=f"reporter-{uuid.uuid4().hex[:8]}",
        scopes=["agent_runs:write"],
        is_active=True,
    )
    session.add(sa)
    await session.flush()
    return sa


@pytest.fixture
def svc(db_session: AsyncSession) -> AgentRunService:
    return AgentRunService(AgentRunRepository(db_session))


def _step(seq: int, **over: Any) -> dict[str, Any]:
    step: dict[str, Any] = {
        "seq": seq,
        "kind": "read",
        "tool": "get_consumer_lag",
        "result_excerpt": f"lag reading {seq}",
        "outcome": "success",
        "latency_ms": 9.0,
        "at": (_T0 + timedelta(seconds=seq)).isoformat(),
    }
    step.update(over)
    return step


_HYPOTHESES = [
    {
        "name": "a stalled consumer",
        "category": "queue",
        "confidence": 0.8,
        "reasoning_excerpt": "lag climbing on one group while the others are flat",
    },
    {"name": "a slow downstream", "confidence": 0.15},
]

_PLAN = {
    "action_tool": "restart_consumer_group",
    "action_arguments": {"consumer_group": "worker-dispatcher"},
    "target_hypothesis": "a stalled consumer",
    "rationale_excerpt": "the group holds its assignment, so a restart is the cheap test",
}

_BUDGET = {"tool_calls_used": 4, "tool_calls_max": 13, "usd_used": 0.31}


# --------------------------------------------------------------------------
# The first report carries all of it
# --------------------------------------------------------------------------


async def test_the_first_report_stores_every_new_field(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    sa = await _service_account(db_session, default_tenant.id)

    outcome = await svc.report_run(
        run_id=uuid.uuid4(),
        tenant_id=default_tenant.id,
        service_account_id=sa.id,
        state=AgentRunState.REMEDIATING.value,
        at=_T0,
        hypotheses=_HYPOTHESES,
        plan=_PLAN,
        verification={"verdict": "not_verified", "attempt": 1, "of": 3},
        step=_step(1),
        budget=_BUDGET,
    )
    run = outcome.run

    assert [h["name"] for h in run.hypotheses] == [
        "a stalled consumer",
        "a slow downstream",
    ]
    assert run.plan["target_hypothesis"] == "a stalled consumer"
    # A verdict lands in both places on the first report, not just the list.
    assert run.verification["attempt"] == 1
    assert [v["attempt"] for v in run.verifications] == [1]
    assert [s["seq"] for s in run.steps] == [1]
    assert run.steps_dropped == 0
    assert run.budget["tool_calls_max"] == 13


async def test_a_run_that_reports_none_of_it_reads_as_empty_not_null(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    """Every list column has a default, so a console needs no per-column defensive
    branch — and an empty list means "none reported", which is a fact."""
    sa = await _service_account(db_session, default_tenant.id)

    run = (
        await svc.report_run(
            run_id=uuid.uuid4(),
            tenant_id=default_tenant.id,
            service_account_id=sa.id,
            state=AgentRunState.TRIAGE.value,
            at=_T0,
        )
    ).run

    assert (run.hypotheses, run.verifications, run.steps) == ([], [], [])
    assert (run.plan, run.verification, run.budget) == (None, None, None)
    assert run.steps_dropped == 0


# --------------------------------------------------------------------------
# Rule 5 — filled in, never cleared (and the two fields that are not)
# --------------------------------------------------------------------------


async def test_a_step_only_report_leaves_the_reasoning_standing(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    """THE assertion for rule 5. The reporter sends one of these after every tool call."""
    sa = await _service_account(db_session, default_tenant.id)
    run_id = uuid.uuid4()
    await svc.report_run(
        run_id=run_id,
        tenant_id=default_tenant.id,
        service_account_id=sa.id,
        state=AgentRunState.REMEDIATING.value,
        at=_T0,
        hypotheses=_HYPOTHESES,
        plan=_PLAN,
        verification={"verdict": "verified", "attempt": 2},
        budget=_BUDGET,
    )

    run = (
        await svc.report_run(
            run_id=run_id,
            tenant_id=default_tenant.id,
            service_account_id=sa.id,
            state=AgentRunState.REMEDIATING.value,
            at=_T0 + timedelta(seconds=1),
            step=_step(1),
        )
    ).run

    assert len(run.hypotheses) == 2
    assert run.plan == _PLAN
    assert run.verification["verdict"] == "verified"
    assert run.budget == _BUDGET
    # ...and the same report's step did land.
    assert [s["seq"] for s in run.steps] == [1]


async def test_the_two_older_fields_are_still_cleared_by_omission(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    """The asymmetry, asserted rather than assumed. `current_hypothesis` and `last_step`
    shipped in WO-R3-312 as the single current reading; changing what omitting them means
    would be a worse surprise than an asymmetry two sentences can explain."""
    sa = await _service_account(db_session, default_tenant.id)
    run_id = uuid.uuid4()
    await svc.report_run(
        run_id=run_id,
        tenant_id=default_tenant.id,
        service_account_id=sa.id,
        state=AgentRunState.INVESTIGATING.value,
        at=_T0,
        current_hypothesis={"name": "a stalled consumer", "confidence": 0.8},
        last_step={"kind": "read", "tool": "get_consumer_lag"},
        hypotheses=_HYPOTHESES,
    )

    run = (
        await svc.report_run(
            run_id=run_id,
            tenant_id=default_tenant.id,
            service_account_id=sa.id,
            state=AgentRunState.INVESTIGATING.value,
            at=_T0 + timedelta(seconds=1),
        )
    ).run

    assert run.current_hypothesis is None
    assert run.last_step is None
    assert len(run.hypotheses) == 2, "the ranked list is not cleared by omission"


async def test_an_empty_list_is_an_answer_and_clears_the_ranking(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    """`None` means "this report says nothing about the list"; `[]` means "the list is
    empty". A caller that has genuinely discarded every explanation can say so."""
    sa = await _service_account(db_session, default_tenant.id)
    run_id = uuid.uuid4()
    await svc.report_run(
        run_id=run_id,
        tenant_id=default_tenant.id,
        service_account_id=sa.id,
        state=AgentRunState.INVESTIGATING.value,
        at=_T0,
        hypotheses=_HYPOTHESES,
    )

    run = (
        await svc.report_run(
            run_id=run_id,
            tenant_id=default_tenant.id,
            service_account_id=sa.id,
            state=AgentRunState.INVESTIGATING.value,
            at=_T0 + timedelta(seconds=1),
            hypotheses=[],
        )
    ).run

    assert run.hypotheses == []


async def test_a_later_plan_replaces_the_one_before_it(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    """A re-plan is a new plan, not a second one: the column is the current decision."""
    sa = await _service_account(db_session, default_tenant.id)
    run_id = uuid.uuid4()
    await svc.report_run(
        run_id=run_id,
        tenant_id=default_tenant.id,
        service_account_id=sa.id,
        state=AgentRunState.PLANNING.value,
        at=_T0,
        plan=_PLAN,
    )

    run = (
        await svc.report_run(
            run_id=run_id,
            tenant_id=default_tenant.id,
            service_account_id=sa.id,
            state=AgentRunState.PLANNING.value,
            at=_T0 + timedelta(seconds=1),
            plan={"action_tool": "replay_dlq_by_ids"},
        )
    ).run

    assert run.plan == {"action_tool": "replay_dlq_by_ids"}


# --------------------------------------------------------------------------
# Rule 6 — the ledgers append, bounded
# --------------------------------------------------------------------------


async def test_the_ledger_keeps_every_step_in_the_order_reported(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    sa = await _service_account(db_session, default_tenant.id)
    run_id = uuid.uuid4()

    for seq in range(1, 6):
        run = (
            await svc.report_run(
                run_id=run_id,
                tenant_id=default_tenant.id,
                service_account_id=sa.id,
                state=AgentRunState.INVESTIGATING.value,
                at=_T0 + timedelta(seconds=seq),
                step=_step(seq),
            )
        ).run

    assert [s["seq"] for s in run.steps] == [1, 2, 3, 4, 5]
    # One entry per call, and the phase history did not grow with it: a step is not a
    # transition.
    assert len(run.phase_history) == 1


async def test_a_repeated_seq_changes_nothing(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    """The reporter is fail-open and may retry a report whose answer it never saw. The
    retry has to be a no-op — a ledger with the same call twice reads as two calls."""
    sa = await _service_account(db_session, default_tenant.id)
    run_id = uuid.uuid4()

    for excerpt in ("the first send", "the retry", "and another retry"):
        run = (
            await svc.report_run(
                run_id=run_id,
                tenant_id=default_tenant.id,
                service_account_id=sa.id,
                state=AgentRunState.INVESTIGATING.value,
                at=_T0,
                step=_step(4, result_excerpt=excerpt),
            )
        ).run

    assert [s["seq"] for s in run.steps] == [4]
    # The entry already stored is left exactly as it was: `seq` identifies one call, so a
    # retry is not a chance to revise what the ledger says about it.
    assert run.steps[0]["result_excerpt"] == "the first send"


async def test_the_step_cap_drops_the_oldest_and_counts_them(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    sa = await _service_account(db_session, default_tenant.id)
    run_id = uuid.uuid4()
    overshoot = 3

    for seq in range(1, STEPS_CAP + overshoot + 1):
        run = (
            await svc.report_run(
                run_id=run_id,
                tenant_id=default_tenant.id,
                service_account_id=sa.id,
                state=AgentRunState.INVESTIGATING.value,
                at=_T0,
                step=_step(seq),
            )
        ).run

    assert len(run.steps) == STEPS_CAP
    assert run.steps_dropped == overshoot
    assert run.steps[0]["seq"] == overshoot + 1
    assert run.steps[-1]["seq"] == STEPS_CAP + overshoot


async def test_every_verdict_is_kept_and_the_newest_is_also_its_own_field(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    """Three polls to reach `verified` is a different story from one, and the latest
    column cannot tell it."""
    sa = await _service_account(db_session, default_tenant.id)
    run_id = uuid.uuid4()

    for attempt, verdict in enumerate(
        ["not_verified", "not_verified", "verified"], start=1
    ):
        run = (
            await svc.report_run(
                run_id=run_id,
                tenant_id=default_tenant.id,
                service_account_id=sa.id,
                state=AgentRunState.VERIFYING.value,
                at=_T0 + timedelta(seconds=attempt),
                verification={"verdict": verdict, "attempt": attempt, "of": 3},
            )
        ).run

    assert [v["verdict"] for v in run.verifications] == [
        "not_verified",
        "not_verified",
        "verified",
    ]
    assert run.verification["verdict"] == "verified"


async def test_the_verdict_list_is_capped(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    sa = await _service_account(db_session, default_tenant.id)
    run_id = uuid.uuid4()

    for attempt in range(1, VERIFICATIONS_CAP + 2):
        run = (
            await svc.report_run(
                run_id=run_id,
                tenant_id=default_tenant.id,
                service_account_id=sa.id,
                state=AgentRunState.VERIFYING.value,
                at=_T0,
                verification={"verdict": "not_verified", "attempt": attempt},
            )
        ).run

    assert len(run.verifications) == VERIFICATIONS_CAP
    assert run.verifications[0]["attempt"] == 2, "the oldest verdict went"


async def test_a_step_on_a_closed_run_is_still_refused(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    """Rule 3 is unchanged by rule 6: a closed run is not reopened, and a late step is a
    report like any other. The refusal is the caller's sequencing mistake, not a lost
    step — the run's ending is what an operator has already read."""
    sa = await _service_account(db_session, default_tenant.id)
    run_id = uuid.uuid4()
    await svc.report_run(
        run_id=run_id,
        tenant_id=default_tenant.id,
        service_account_id=sa.id,
        state=AgentRunState.RESOLVED.value,
        at=_T0,
        step=_step(1),
    )

    with pytest.raises(AgentRunAlreadyFinishedError):
        await svc.report_run(
            run_id=run_id,
            tenant_id=default_tenant.id,
            service_account_id=sa.id,
            state=AgentRunState.RESOLVED.value,
            at=_T0 + timedelta(seconds=1),
            step=_step(2),
        )


async def test_the_closing_report_may_carry_a_step(
    db_session: AsyncSession, default_tenant, svc: AgentRunService
) -> None:
    """The last call of a run and the terminal state arrive together, so the report that
    closes the run has to be able to carry both."""
    sa = await _service_account(db_session, default_tenant.id)
    run_id = uuid.uuid4()
    await svc.report_run(
        run_id=run_id,
        tenant_id=default_tenant.id,
        service_account_id=sa.id,
        state=AgentRunState.VERIFYING.value,
        at=_T0,
        step=_step(1),
    )

    run = (
        await svc.report_run(
            run_id=run_id,
            tenant_id=default_tenant.id,
            service_account_id=sa.id,
            state=AgentRunState.RESOLVED.value,
            at=_T0 + timedelta(seconds=1),
            step=_step(2, kind="report"),
            budget={"tool_calls_used": 6},
        )
    ).run

    assert run.finished_at is not None
    assert [s["seq"] for s in run.steps] == [1, 2]
    assert run.budget == {"tool_calls_used": 6}


# --------------------------------------------------------------------------
# The wire: the limits refuse, they do not truncate
# --------------------------------------------------------------------------


def _input(**fields: Any) -> Any:
    from app.mcp.tools.commander_runs import ReportAgentRunInput

    return ReportAgentRunInput(
        run_id=uuid.uuid4(), state="investigating", **fields
    )


def test_a_full_report_is_accepted_on_the_wire() -> None:
    parsed = _input(
        hypotheses=_HYPOTHESES,
        plan=_PLAN,
        verification={
            "verdict": "verified_stabilizer",
            "reasoning_excerpt": "lag drained to 0 and stayed there",
            "attempt": 2,
            "of": 3,
        },
        step=_step(1, kind="action", tool="restart_consumer_group"),
        budget=_BUDGET,
    )

    assert parsed.hypotheses is not None
    assert parsed.hypotheses[0].reasoning_excerpt is not None
    assert parsed.plan is not None and parsed.plan.action_tool == (
        "restart_consumer_group"
    )
    # An open vocabulary: the platform has no opinion about what counts as verified.
    assert parsed.verification is not None
    assert parsed.verification.verdict == "verified_stabilizer"
    assert parsed.step is not None and parsed.step.kind == "action"


@pytest.mark.parametrize(
    ("field", "payload"),
    [
        (
            "hypotheses",
            [{"name": "n", "reasoning_excerpt": "x" * 281}],
        ),
        ("plan", {"action_tool": "t", "rationale_excerpt": "x" * 281}),
        ("verification", {"verdict": "verified", "reasoning_excerpt": "x" * 281}),
    ],
)
def test_an_over_long_reasoning_excerpt_is_refused(field: str, payload: Any) -> None:
    """Refused, not truncated: a silent cut stores something the caller did not write,
    and a caller that meant to send a summary and sent a page never finds out."""
    with pytest.raises(ValidationError):
        _input(**{field: payload})


def test_an_over_long_result_excerpt_is_refused() -> None:
    """400 characters, and the refusal is what keeps this table from becoming a store of
    whole tool outputs (ADR 0037's rejected option)."""
    with pytest.raises(ValidationError):
        _input(step=_step(1, result_excerpt="x" * 401))

    assert _input(step=_step(1, result_excerpt="x" * 400)).step is not None


def test_a_step_needs_a_seq_and_a_kind() -> None:
    """`seq` is the step's identity — without it a retry cannot be told from a second
    call — so it is the one field in the object with no default."""
    with pytest.raises(ValidationError):
        _input(step={"kind": "read", "tool": "get_consumer_lag"})
    with pytest.raises(ValidationError):
        _input(step={"seq": 1, "tool": "get_consumer_lag"})
    with pytest.raises(ValidationError):
        _input(step={"seq": -1, "kind": "read"})


def test_a_step_may_be_a_status_report_and_a_last_step_may_not() -> None:
    """The one place the two step vocabularies differ, and the reason: a status report
    explains a gap between two calls, which is a ledger entry and was never the caller's
    "most recent step"."""
    assert _input(step={"seq": 1, "kind": "report"}).step is not None
    with pytest.raises(ValidationError):
        _input(last_step={"kind": "report", "tool": "report_agent_run"})


def test_a_typo_in_a_nested_field_is_a_refusal_not_a_dropped_value() -> None:
    """`extra="forbid"` on every new object, as on the two that shipped: a report that
    silently lost a field would show up as a panel that is quietly wrong."""
    with pytest.raises(ValidationError):
        _input(plan={"action_tool": "t", "rational_excerpt": "typo"})
    with pytest.raises(ValidationError):
        _input(budget={"tool_calls_used": 1, "tool_calls_remaining": 3})


def test_the_numbers_are_bounded_where_a_negative_would_be_nonsense() -> None:
    with pytest.raises(ValidationError):
        _input(budget={"usd_used": -1.0})
    with pytest.raises(ValidationError):
        _input(hypotheses=[{"name": "n", "confidence": 1.4}])
    with pytest.raises(ValidationError):
        _input(verification={"verdict": "verified", "attempt": 0})
