"""
The write side of `agent_runs` — the rules the two report tools share (ADR 0035).

Four of them, and each one exists because a console reads this table live:

1. **Upsert by run id.** A report is idempotent: the same state reported twice leaves
   the row as it was, so a caller that retries after a timeout cannot double-count.
2. **Phase history is append-only, and it appends on a state *change*.** One entry per
   transition, oldest first; nothing rewrites or removes an entry. A repeat of the
   current state adds nothing — otherwise a caller reporting every loop iteration would
   turn a timeline into a call log.
3. **A terminal state closes the run.** `finished_at` is stamped the first time `state`
   reaches one, and a later report on a closed run is refused (409) rather than allowed
   to rewind what an operator is looking at.
4. **The briefing lands once.** A second write is refused (409), never merged.

WO-R3-328 adds two rules of the same kind, for the columns that carry the reasoning
(ADR 0037):

5. **A reading is filled in, never cleared.** `hypotheses`, `plan`, `verification` and
   `budget` are replaced when a report carries them and left alone when it does not.
   This is deliberately NOT how `current_hypothesis` and `last_step` behave: those are
   WO-R3-312's fields and stay replace-or-clear. The reporter sends a step-only report
   after every tool call, and clearing on omission would blank a panel an operator is
   reading — the failure the order exists to remove, in a new place.
6. **The ledgers append, bounded, and say what a bound cost.** One `step` per call,
   de-duplicated by `seq` so a fail-open retry cannot double-count; `steps` is capped at
   `STEPS_CAP` and `verifications` at `VERIFICATIONS_CAP`, oldest dropped first, and
   `steps_dropped` counts what went. A capped list a reader knows is capped is useful;
   one it does not is a run that looks shorter than it was.

Nothing here interprets the report. Every value is stored as the caller sent it.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.exceptions import ConflictError
from app.models.agent_run import AgentRun
from app.models.enums import TERMINAL_AGENT_RUN_STATES
from app.repositories.agent_run import AgentRunRepository

#: Error codes the two tools return, and the commander's client buckets on.
ERROR_RUN_ALREADY_FINISHED = "agent_run_already_finished"
ERROR_BRIEFING_ALREADY_RECORDED = "agent_run_briefing_already_recorded"
ERROR_RUN_NOT_FOUND = "agent_run_not_found"

#: How many steps one run keeps. A capped 13-call run is a run with room to spare; the
#: cap is there so a caller in a loop cannot grow one row without bound. Oldest go
#: first, and `steps_dropped` records how many.
STEPS_CAP = 200

#: How many verify verdicts one run keeps. A remediation polls a handful of times, so
#: this is a bound rather than a budget; the oldest go first.
VERIFICATIONS_CAP = 50


class AgentRunAlreadyFinishedError(ConflictError):
    error_code = ERROR_RUN_ALREADY_FINISHED


class AgentRunBriefingAlreadyRecordedError(ConflictError):
    error_code = ERROR_BRIEFING_ALREADY_RECORDED


class AgentRunNotFoundError(ConflictError):
    """409, not 404: the caller chose the id, so "no such run" is a sequencing
    mistake on its side (a briefing before the first state report), not a bad URL."""

    error_code = ERROR_RUN_NOT_FOUND


class ReportOutcome:
    """What one report did, for the tool's own output model."""

    __slots__ = ("run", "created", "phase_appended")

    def __init__(self, run: AgentRun, *, created: bool, phase_appended: bool) -> None:
        self.run = run
        self.created = created
        self.phase_appended = phase_appended


class AgentRunService:
    def __init__(self, runs: AgentRunRepository) -> None:
        self._runs = runs

    async def report_run(
        self,
        *,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        service_account_id: uuid.UUID,
        state: str,
        at: datetime,
        alert_id: uuid.UUID | None = None,
        scenario: str | None = None,
        current_hypothesis: dict[str, Any] | None = None,
        last_step: dict[str, Any] | None = None,
        hypotheses: list[dict[str, Any]] | None = None,
        plan: dict[str, Any] | None = None,
        verification: dict[str, Any] | None = None,
        step: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
    ) -> ReportOutcome:
        """Create or update one run. See the module docstring for the six rules."""
        existing = await self._runs.get_for_tenant(run_id, tenant_id)
        terminal = state in TERMINAL_AGENT_RUN_STATES

        if existing is None:
            run = AgentRun(
                id=run_id,
                tenant_id=tenant_id,
                alert_id=alert_id,
                service_account_id=service_account_id,
                scenario=scenario,
                state=state,
                phase_history=[_entry(state, at)],
                current_hypothesis=current_hypothesis,
                last_step=last_step,
                hypotheses=list(hypotheses or []),
                plan=plan,
                verification=verification,
                verifications=[verification] if verification is not None else [],
                steps=[step] if step is not None else [],
                steps_dropped=0,
                budget=budget,
                finished_at=at if terminal else None,
            )
            self._runs.session.add(run)
            await self._runs.session.flush()
            # `started_at` / `updated_at` are server defaults, so the object does not
            # carry them until it is read back — and the receipt reports them.
            await self._runs.session.refresh(run)
            return ReportOutcome(run, created=True, phase_appended=True)

        if existing.finished_at is not None:
            raise AgentRunAlreadyFinishedError(
                f"run {run_id} already reached a terminal state "
                f"({existing.state}); a closed run is not reopened"
            )

        appended = existing.state != state
        if appended:
            # Rebound, not mutated in place: SQLAlchemy does not track in-place
            # changes to a JSON column, so `.append()` would be silently dropped.
            existing.phase_history = [*(existing.phase_history or []), _entry(state, at)]
            existing.state = state
        # WO-R3-312's two fields: the newest reading of a moving value, so it replaces —
        # including with nothing. Kept as they shipped.
        existing.current_hypothesis = current_hypothesis
        existing.last_step = last_step
        # Only ever filled in, never cleared: a caller that names the alert later
        # tells us something, a caller that stops naming it does not.
        if alert_id is not None:
            existing.alert_id = alert_id
        if scenario is not None:
            existing.scenario = scenario
        # Rule 5 — the reasoning columns are filled in and never cleared (see the module
        # docstring for why this differs from the two above).
        if hypotheses is not None:
            existing.hypotheses = list(hypotheses)
        if plan is not None:
            existing.plan = plan
        if budget is not None:
            existing.budget = budget
        if verification is not None:
            existing.verification = verification
            # Rebound, not appended in place: SQLAlchemy does not track in-place changes
            # to a JSON column (the same trap `phase_history` documents).
            existing.verifications = _capped(
                [*(existing.verifications or []), verification], VERIFICATIONS_CAP
            )
        if step is not None:
            kept, dropped = _append_step(existing.steps or [], step)
            existing.steps = kept
            existing.steps_dropped = (existing.steps_dropped or 0) + dropped
        if terminal:
            existing.finished_at = at
        await self._runs.session.flush()
        # `updated_at` moves on the server (`onupdate`), so read it back rather than
        # reporting the value this session happened to be holding.
        await self._runs.session.refresh(existing)
        return ReportOutcome(existing, created=False, phase_appended=appended)

    async def report_briefing(
        self,
        *,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        briefing: dict[str, Any],
        prose: str | None,
        at: datetime,
    ) -> AgentRun:
        """Attach the run's write-up. Once — a second call is a 409.

        Does not touch `state`: the briefing describes a run whose state the caller
        has already reported, and inferring one from the write-up would be the
        platform interpreting the report.
        """
        run = await self._runs.get_for_tenant(run_id, tenant_id)
        if run is None:
            raise AgentRunNotFoundError(
                f"no run {run_id} to attach a briefing to; report its state first"
            )
        if run.briefing is not None:
            raise AgentRunBriefingAlreadyRecordedError(
                f"run {run_id} already has a briefing; it is written once"
            )
        # `prose` rides inside the stored object rather than beside it: it is part of
        # the write-up, and one column means a reader cannot see half of it.
        run.briefing = {**briefing, "prose": prose, "recorded_at": _iso(at)}
        # A briefing arrives at the end, so close the run if the caller's last state
        # report did not. Never re-stamped.
        if run.finished_at is None:
            run.finished_at = at
        await self._runs.session.flush()
        await self._runs.session.refresh(run)
        return run


def _entry(state: str, at: datetime) -> dict[str, Any]:
    return {"state": state, "at": _iso(at)}


def _capped(entries: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    """The newest `cap` entries, order preserved. Oldest go first: the end of a run is
    what an operator is looking at."""
    return entries[-cap:] if len(entries) > cap else entries


def _append_step(steps: list[dict[str, Any]], step: dict[str, Any]) -> tuple[
    list[dict[str, Any]], int
]:
    """The ledger after appending `step`, and how many entries the cap discarded.

    A `seq` already in the ledger is a repeat — the reporter is fail-open and may retry a
    report it never saw answered — so it changes nothing rather than appearing twice.
    A step with no `seq` cannot be de-duplicated and is appended as given; the wire model
    requires one, so that is a caller talking to the service layer directly.
    """
    seq = step.get("seq")
    if seq is not None and any(existing.get("seq") == seq for existing in steps):
        return steps, 0
    grown = [*steps, step]
    dropped = max(0, len(grown) - STEPS_CAP)
    return _capped(grown, STEPS_CAP), dropped


def _iso(at: datetime) -> str:
    """ISO-8601 UTC. Stored as text because JSON has no date type, and a naive stamp
    is read as UTC — every clock in this platform is."""
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return at.astimezone(UTC).isoformat()


__all__ = [
    "ERROR_BRIEFING_ALREADY_RECORDED",
    "ERROR_RUN_ALREADY_FINISHED",
    "ERROR_RUN_NOT_FOUND",
    "STEPS_CAP",
    "VERIFICATIONS_CAP",
    "AgentRunAlreadyFinishedError",
    "AgentRunBriefingAlreadyRecordedError",
    "AgentRunNotFoundError",
    "AgentRunService",
    "ReportOutcome",
]
