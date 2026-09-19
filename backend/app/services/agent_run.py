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

Nothing here interprets the report. `state`, `current_hypothesis`, `last_step` and
`briefing` are stored as the caller sent them.
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
    ) -> ReportOutcome:
        """Create or update one run. See the module docstring for the four rules."""
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
        # The rest is the newest reading of a moving value, so it replaces.
        existing.current_hypothesis = current_hypothesis
        existing.last_step = last_step
        # Only ever filled in, never cleared: a caller that names the alert later
        # tells us something, a caller that stops naming it does not.
        if alert_id is not None:
            existing.alert_id = alert_id
        if scenario is not None:
            existing.scenario = scenario
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
    "AgentRunAlreadyFinishedError",
    "AgentRunBriefingAlreadyRecordedError",
    "AgentRunNotFoundError",
    "AgentRunService",
    "ReportOutcome",
]
