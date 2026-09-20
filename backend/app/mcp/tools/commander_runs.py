"""
`report_agent_run` / `report_agent_briefing` — the responder's own report of its run.

Two writes and no read. The platform stores what the caller says it is doing so a human
operator can watch a live incident, and offers the caller nothing back beyond a receipt
(ADR 0035). Both carry `agent_runs:write` and the `[commander: telemetry]` description
prefix, which is how the caller's planner drops them from the tool list it offers a
model: no model chooses these calls, a loop makes them at a fixed point in its cycle.

Model docstrings are deliberately absent below: Pydantic copies a class docstring into
the JSON Schema's top-level `description`, and these schemas are pinned in the caller's
contract snapshot (plat #210).
"""

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from app.core.logging import get_logger
from app.mcp.commander import commander_tool
from app.mcp.registry import ToolContext
from app.models.enums import AgentRunState
from app.repositories.agent_run import AgentRunRepository
from app.services.agent_run import AgentRunService
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)

#: The states a caller may report, as a closed set on the wire. Spelled out rather than
#: derived so the JSON Schema shows the whole vocabulary to whoever reads it.
ReportableState = Literal[
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

#: Kinds of step a caller may describe. `read` is a call that only observed; `action` is
#: one that changed the platform.
StepKind = Literal["read", "action"]

#: Kinds of entry the step LEDGER carries. Wider than `StepKind` by one member: a status
#: report is a real entry in the ledger (it explains a gap between two calls) and was
#: never a `last_step`, so the narrower vocabulary above does not move.
StepEventKind = Literal["read", "action", "report"]


class HypothesisReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        max_length=128,
        description="The caller's own short name for its leading explanation. Free "
        "text — the platform has no list of valid names and does not check this one.",
    )
    category: str | None = Field(
        default=None,
        max_length=64,
        description="The caller's coarse grouping for that explanation, when it has "
        "one. `null` means the caller did not supply one, never that it has no "
        "explanation — an absent explanation is a `null` hypothesis instead.",
    )
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="How sure the caller says it is, 0.0 to 1.0. `null` when it did "
        "not say. Stored and shown as given; the platform never scales or compares it.",
    )


class StepReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: StepKind = Field(
        description="`read` when the step only observed the platform, `action` when it "
        "changed something."
    )
    tool: str = Field(
        max_length=128,
        description="Name of the call the step made, as the caller knows it.",
    )
    at: datetime | None = Field(
        default=None,
        description="When the step happened — ISO-8601. `null` when the caller did not "
        "say, which is not the same as 'just now': the platform does not fill it in.",
    )


class RankedHypothesisReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        max_length=128,
        description="The caller's own short name for this explanation. Free text — the "
        "platform has no list of valid names and does not check this one.",
    )
    category: str | None = Field(
        default=None,
        max_length=64,
        description="The caller's coarse grouping for this explanation, when it has "
        "one. `null` means it did not supply one.",
    )
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="How sure the caller says it is about this one, 0.0 to 1.0. `null` "
        "when it did not say. Stored and shown as given; the platform never scales one, "
        "compares two, or re-ranks the list by them — the ORDER of the list is the "
        "ranking.",
    )
    reasoning_excerpt: str | None = Field(
        default=None,
        max_length=280,
        description="A short extract of why the caller holds this explanation — an "
        "EXCERPT, truncated by the caller, not the reasoning itself. Over 280 "
        "characters is refused rather than silently cut, so what is stored is always "
        "what the caller meant to send. `null` when it sent none.",
    )


class PlanReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_tool: str = Field(
        max_length=128,
        description="The call the caller intends to make, by name.",
    )
    action_arguments: dict[str, Any] | None = Field(
        default=None,
        description="The arguments it intends to send, as an object. Stored verbatim "
        "and validated nowhere — it is the caller's own plan, not a call. `null` when it "
        "did not say.",
    )
    target_hypothesis: str | None = Field(
        default=None,
        max_length=128,
        description="Which explanation this action is meant to settle, by the `name` "
        "the caller gave it. `null` when the plan names none; the platform does not "
        "check it against the list.",
    )
    rationale_excerpt: str | None = Field(
        default=None,
        max_length=280,
        description="A short extract of why this action, and not another. An EXCERPT, "
        "truncated by the caller; over 280 characters is refused rather than cut.",
    )


class VerificationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: str = Field(
        max_length=64,
        description="What the caller concluded from this check, in its own vocabulary — "
        "`verified`, `not_verified`, `verified_stabilizer` and anything else it uses. "
        "Deliberately NOT a closed set: the platform has no opinion about what counts as "
        "verified and a fixed list here would refuse a verdict the caller has.",
    )
    reasoning_excerpt: str | None = Field(
        default=None,
        max_length=280,
        description="A short extract of what the caller read to reach that verdict. An "
        "EXCERPT, truncated by the caller; over 280 characters is refused rather than "
        "cut.",
    )
    attempt: int | None = Field(
        default=None,
        ge=1,
        description="Which check this was, counting from 1. `null` when the caller did "
        "not say — never inferred from how many it has already sent, because a report "
        "can be lost.",
    )
    of: int | None = Field(
        default=None,
        ge=1,
        description="How many checks the caller intends to make in total, when it knows. "
        "`null` when it did not say.",
    )


class StepEventReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int = Field(
        ge=0,
        description="The caller's own position for this step in its run, increasing. It "
        "is the identity of the step: reporting a `seq` already stored changes nothing, "
        "so retrying a report that timed out cannot enter the same step twice, and an "
        "operator polling for new steps asks for everything after the last `seq` it saw.",
    )
    kind: StepEventKind = Field(
        description="`read` when the step only observed the platform, `action` when it "
        "changed something, `report` when it was a status report like this one — which "
        "is worth an entry because it explains a gap between two calls."
    )
    tool: str | None = Field(
        default=None,
        max_length=128,
        description="Name of the call the step made, as the caller knows it. `null` for "
        "a step that was not a call.",
    )
    arguments: dict[str, Any] | None = Field(
        default=None,
        description="What the step sent, as an object, stored verbatim. `null` when the "
        "caller sent none. Do not put anything here you would not want a human operator "
        "reading on a screen.",
    )
    result_excerpt: str | None = Field(
        default=None,
        max_length=400,
        description="A short extract of what the step got back — an EXCERPT, truncated "
        "by the caller, never the whole answer. Over 400 characters is refused rather "
        "than cut, so this table cannot become a store of full tool output. `null` when "
        "the caller sent none.",
    )
    outcome: str | None = Field(
        default=None,
        max_length=64,
        description="How the step ended, in the caller's own word — `success`, `error` "
        "and whatever else it uses. Not a closed set and not checked.",
    )
    latency_ms: float | None = Field(
        default=None,
        ge=0.0,
        description="How long the step took, in milliseconds, on the caller's clock. "
        "`null` when it did not say.",
    )
    at: datetime | None = Field(
        default=None,
        description="When the step happened — ISO-8601. `null` when the caller did not "
        "say, which is not the same as 'just now': the platform does not fill it in.",
    )


class BudgetReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_calls_used: int | None = Field(
        default=None,
        ge=0,
        description="Calls the caller has spent so far, on its own count. The platform "
        "does not count them and never corrects this.",
    )
    tool_calls_max: int | None = Field(
        default=None,
        ge=0,
        description="The ceiling the caller is working to. `null` when it has none or "
        "did not say — never 0 for 'no limit', because 0 reads as 'no calls left'.",
    )
    tokens_used: int | None = Field(
        default=None, ge=0, description="Tokens spent so far, on the caller's count."
    )
    usd_used: float | None = Field(
        default=None,
        ge=0.0,
        description="Money spent so far, in US dollars, on the caller's count.",
    )
    wall_seconds: float | None = Field(
        default=None,
        ge=0.0,
        description="How long the run has been going, in seconds, on the caller's clock.",
    )


class ReportAgentRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID = Field(
        description="The caller's own id for this run, stable across every report in "
        "it. This call is an upsert on it: the first report creates the record, each "
        "later one updates the same record. Reporting the same state twice changes "
        "nothing, so a retry after a timeout is safe."
    )
    state: ReportableState = Field(
        description="Where the caller is now. `resolved`, `escalated` and `failed` are "
        "terminal: reporting one closes the run, and a further report on a closed run "
        "is refused with `agent_run_already_finished` rather than reopening it."
    )
    at: datetime | None = Field(
        default=None,
        description="When this transition happened — ISO-8601. Omit and the platform "
        "stamps its own clock at the moment it stores the report, which is the honest "
        "reading it has; it never guesses a time the caller did not send.",
    )
    alert_id: uuid.UUID | None = Field(
        default=None,
        description="The alert this run answers, when there is one. Recorded the first "
        "time it is supplied and never unset by a later report that omits it — a "
        "caller that names it later is telling us something, one that stops naming it "
        "is not. Unknown ids are stored as given and read back as `null`.",
    )
    run_label: str | None = Field(
        default=None,
        max_length=128,
        description="A short stable name for this run, for an operator reading the "
        "record afterwards. Free text, treated the same way as `alert_id`: filled in "
        "when supplied, never cleared by a later report.",
    )
    current_hypothesis: HypothesisReport | None = Field(
        default=None,
        description="The caller's leading explanation right now, or `null` when it has "
        "none. Replaced wholesale on every report — this is the current reading, not a "
        "history — so omitting it clears what was there.",
    )
    last_step: StepReport | None = Field(
        default=None,
        description="The most recent step the caller took, or `null` before its first. "
        "Replaced wholesale on every report, like `current_hypothesis`.",
    )
    hypotheses: list[RankedHypothesisReport] | None = Field(
        default=None,
        description="Every explanation the caller is holding, BEST FIRST — the order is "
        "the ranking. Replaced whole when this report carries it and never cleared by a "
        "later report that omits it, which is the opposite of `current_hypothesis` and is "
        "deliberate: a report about one step should not blank the list a person is "
        "reading. Send `[]` to say the list is genuinely empty.",
    )
    plan: PlanReport | None = Field(
        default=None,
        description="What the caller has decided to do, and why. Sent when it decides — "
        "on the way into `remediating`. The newest one replaces the one before it (a "
        "re-plan is a new plan) and never cleared by a later report that omits it.",
    )
    verification: VerificationReport | None = Field(
        default=None,
        description="One verdict from one check of whether the action worked. Send one "
        "per check: the newest is kept on its own for a reader that wants one line, AND "
        "appended to the list of every verdict in this run, because three checks to "
        "reach `verified` is a different story from one. Never cleared by a later report "
        "that omits it.",
    )
    step: StepEventReport | None = Field(
        default=None,
        description="ONE STEP PER CALL, appended to this run's step ledger in the order "
        "reported. Not replaced, not merged: a second step in one report is not "
        "expressible, which is what keeps the ledger a ledger. `seq` is the identity — a "
        "repeat of one already stored changes nothing. The ledger keeps the newest 200 "
        "steps; past that the oldest are dropped and `steps_dropped` in the reply counts "
        "them, so a reader is never shown a shortened run as a complete one.",
    )
    budget: BudgetReport | None = Field(
        default=None,
        description="What this run has spent so far, on the caller's own meters. The "
        "newest reading replaces the one before it and is never cleared by a later report "
        "that omits it, so the last reading stands. The platform counts nothing here and "
        "corrects nothing.",
    )


class ReportAgentRunOutput(BaseModel):
    run_id: uuid.UUID = Field(
        description="The run this report landed on — the id that was sent."
    )
    state: str = Field(description="The state now stored for the run.")
    created: bool = Field(
        description="True when this report created the record, false when it updated "
        "one that already existed."
    )
    phase_appended: bool = Field(
        description="True when the state changed and a phase entry was appended. False "
        "means the state was already this one and the history was left alone — the "
        "report still landed, and `accepted` says so."
    )
    phase_count: int = Field(
        description="How many phase entries the run now has. Never decreases: the "
        "history is append-only."
    )
    started_at: datetime = Field(
        description="When the first report of this run landed, on the platform's clock."
    )
    updated_at: datetime = Field(
        description="When this report landed, on the platform's clock."
    )
    finished_at: datetime | None = Field(
        default=None,
        description="When the run reached a terminal state. `null` while it has not — "
        "which means the run is still open, not that its end was not recorded.",
    )
    steps_count: int = Field(
        description="How many steps the run's ledger holds now, after this report and "
        "after the cap. Not how many were reported — subtract nothing; add "
        "`steps_dropped` for that."
    )
    steps_dropped: int = Field(
        description="How many of the oldest steps the cap has discarded over this run's "
        "life. Never decreases. 0 means the ledger is the whole run; anything else means "
        "it is the newest part of it."
    )
    accepted: bool = Field(
        description="Always true in a successful reply; a refused report is an error, "
        "not an `accepted: false`. Present so a caller logging one field logs a "
        "meaningful one."
    )


class ReportAgentBriefingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID = Field(
        description="The run this write-up belongs to. The run must already exist — "
        "report its state first, or this is refused with `agent_run_not_found`."
    )
    briefing: dict[str, Any] = Field(
        description="The caller's own end-of-run write-up, as a JSON object. Stored "
        "verbatim and never interpreted, so its shape is the caller's to choose and "
        "the platform validates nothing inside it. Written ONCE per run: a second "
        "call is refused with `agent_run_briefing_already_recorded` rather than "
        "merged, so what an operator has read cannot change underneath them."
    )
    prose: str | None = Field(
        default=None,
        max_length=20000,
        description="The written-out version, when the caller produced one. Stored "
        "inside the object above under `prose`, so a reader cannot be shown half the "
        "write-up. `null` when there is none.",
    )
    at: datetime | None = Field(
        default=None,
        description="When the write-up was produced — ISO-8601. Omit and the platform "
        "stamps its own clock, as `report_agent_run` does.",
    )


class ReportAgentBriefingOutput(BaseModel):
    run_id: uuid.UUID = Field(description="The run the write-up landed on.")
    state: str = Field(
        description="The run's state, unchanged by this call: a write-up describes a "
        "run whose state the caller has already reported, and the platform does not "
        "infer one from it."
    )
    recorded_at: datetime = Field(
        description="The time stored with the write-up — `at` when it was sent, "
        "otherwise the platform's clock when it landed."
    )
    finished_at: datetime = Field(
        description="When the run closed. A write-up arrives at the end, so this is "
        "never null: if the caller never reported a terminal state, this call sets it."
    )
    accepted: bool = Field(description="Always true in a successful reply.")


def _service(ctx: ToolContext) -> AgentRunService:
    return AgentRunService(AgentRunRepository(ctx.db))


def _dumped(model: BaseModel | None) -> dict[str, Any] | None:
    """JSON-mode dump, or `None` for a field this report did not carry. The service
    layer tells the two apart: `None` leaves the column alone."""
    return model.model_dump(mode="json") if model is not None else None


def _at(supplied: datetime | None) -> datetime:
    """The caller's time when it sent one, the platform's clock otherwise. Naive input
    is read as UTC — every clock in this platform is."""
    if supplied is None:
        return datetime.now(UTC)
    if supplied.tzinfo is None:
        return supplied.replace(tzinfo=UTC)
    return supplied


@commander_tool(
    "report_agent_run",
    description=(
        "Record where you are in the incident you are working, so a human "
        "operator watching the console can see it. This is a write with no "
        "matching read: nothing here can be read back, and there is no tool "
        "that returns what was reported.\n"
        "UPSERT BY `run_id`. The first call for an id creates the record; "
        "every later call with the same id updates it. Reporting the same "
        "state twice leaves the record exactly as it was, so retrying after "
        "a timeout or a dropped connection is safe and cannot double-count.\n"
        "WHAT IS KEPT AND WHAT IS REPLACED, in three groups.\n"
        "  - APPENDED: a `state` change adds one entry to an append-only "
        "phase history (one entry per transition, so a timeline no later "
        "report can rewrite); each `step` is added to the run's step "
        "ledger; each `verification` is added to the list of verdicts. "
        "One step and one verdict per call.\n"
        "  - REPLACED BUT NEVER CLEARED: `hypotheses`, `plan`, "
        "`verification` (its latest), `budget`, `alert_id` and "
        "`run_label`. A report that carries one replaces it; a report "
        "that omits one leaves what was there. So a report about a single "
        "step does not erase the reasoning reported with the last "
        "transition.\n"
        "  - REPLACED, INCLUDING WITH NOTHING: `current_hypothesis` and "
        "`last_step`, which are the single current reading and are "
        "cleared by omitting them.\n"
        "EXCERPTS, NOT OUTPUT. `reasoning_excerpt`, `rationale_excerpt` "
        "and `result_excerpt` are short extracts YOU truncate — 280, 280 "
        "and 400 characters. Over the limit is refused rather than cut, "
        "so what is stored is always what you meant to send. A human "
        "reads these on a screen.\n"
        "TERMINAL STATES CLOSE THE RUN. `resolved`, `escalated` and `failed` "
        "stamp the run finished. A further report on a finished run is "
        "refused with `agent_run_already_finished`: a closed run is not "
        "reopened, because an operator may already have read its ending.\n"
        "WHICH CLOCK. `at` is your clock and is stored as sent; "
        "`started_at`, `updated_at` and `finished_at` in the reply are the "
        "platform's. Omit `at` and the platform stamps its own time rather "
        "than inventing yours.\n"
        "THIS IS NOT A TOOL TO CHOOSE. It changes nothing about the incident "
        "and tells you nothing you did not already know — it exists so a "
        "person can follow along. Calling it is not progress and not "
        "investigation.\n"
        "IT CANNOT FAIL YOUR WORK. A refusal here means the report was not "
        "stored; the incident, and everything you have done to it, is "
        "untouched."
    ),
    input_model=ReportAgentRunInput,
    output_model=ReportAgentRunOutput,
)
async def report_agent_run(
    inp: ReportAgentRunInput, ctx: ToolContext
) -> ReportAgentRunOutput:
    assert ctx.principal.service_account is not None, (
        "agent_runs:write is a machine-principal scope; a human cannot reach this tool"
    )
    outcome = await _service(ctx).report_run(
        run_id=inp.run_id,
        tenant_id=ctx.principal.tenant_id,
        service_account_id=ctx.principal.service_account.id,
        state=AgentRunState(inp.state).value,
        at=_at(inp.at),
        alert_id=inp.alert_id,
        scenario=inp.run_label,
        current_hypothesis=(
            inp.current_hypothesis.model_dump(mode="json")
            if inp.current_hypothesis is not None
            else None
        ),
        last_step=(
            inp.last_step.model_dump(mode="json") if inp.last_step is not None else None
        ),
        # `None` and `[]` are different answers here: the first says "this report has
        # nothing to say about the list", the second says "the list is empty".
        hypotheses=(
            [h.model_dump(mode="json") for h in inp.hypotheses]
            if inp.hypotheses is not None
            else None
        ),
        plan=_dumped(inp.plan),
        verification=_dumped(inp.verification),
        step=_dumped(inp.step),
        budget=_dumped(inp.budget),
    )
    run = outcome.run
    return ReportAgentRunOutput(
        run_id=run.id,
        state=run.state,
        created=outcome.created,
        phase_appended=outcome.phase_appended,
        phase_count=len(run.phase_history or []),
        started_at=run.started_at,
        updated_at=run.updated_at,
        finished_at=run.finished_at,
        steps_count=len(run.steps or []),
        steps_dropped=run.steps_dropped or 0,
        accepted=True,
    )


@commander_tool(
    "report_agent_briefing",
    description=(
        "Record your end-of-run write-up for the human operator reading the "
        "console. Like `report_agent_run`, a write with no matching read.\n"
        "ONCE PER RUN. A second call for the same `run_id` is refused with "
        "`agent_run_briefing_already_recorded` — the write-up is not merged "
        "or overwritten, because an operator may already have read it. If "
        "you need to say more, say it in the one call.\n"
        "THE RUN MUST EXIST. Report a state for the run first; a write-up "
        "for an unknown id is refused with `agent_run_not_found` rather "
        "than creating a record with no history.\n"
        "STORED VERBATIM. `briefing` is a JSON object of your own shape. "
        "The platform stores it as sent, validates nothing inside it and "
        "interprets none of it; `prose` is stored inside that object so a "
        "reader cannot be shown half of it.\n"
        "IT CLOSES THE RUN. A write-up arrives at the end, so this call "
        "stamps the run finished if your last state report did not.\n"
        "THIS IS NOT A TOOL TO CHOOSE, and a refusal here changes nothing "
        "about the incident or about what you have already done to it."
    ),
    input_model=ReportAgentBriefingInput,
    output_model=ReportAgentBriefingOutput,
)
async def report_agent_briefing(
    inp: ReportAgentBriefingInput, ctx: ToolContext
) -> ReportAgentBriefingOutput:
    at = _at(inp.at)
    run = await _service(ctx).report_briefing(
        run_id=inp.run_id,
        tenant_id=ctx.principal.tenant_id,
        briefing=inp.briefing,
        prose=inp.prose,
        at=at,
    )
    assert run.finished_at is not None  # report_briefing always closes the run
    return ReportAgentBriefingOutput(
        run_id=run.id,
        state=run.state,
        recorded_at=at,
        finished_at=run.finished_at,
        accepted=True,
    )


__all__ = [
    "BudgetReport",
    "HypothesisReport",
    "PlanReport",
    "RankedHypothesisReport",
    "ReportAgentBriefingInput",
    "ReportAgentBriefingOutput",
    "ReportAgentRunInput",
    "ReportAgentRunOutput",
    "ReportableState",
    "StepEventKind",
    "StepEventReport",
    "StepReport",
    "VerificationReport",
    "report_agent_briefing",
    "report_agent_run",
]
