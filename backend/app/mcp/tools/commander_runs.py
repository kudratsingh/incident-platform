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
        "WHAT IS KEPT AND WHAT IS REPLACED. `state` changes append one entry "
        "to an append-only phase history — one entry per transition, so the "
        "console can draw a timeline no later report can rewrite. "
        "`current_hypothesis` and `last_step` are the current reading and are "
        "REPLACED on every call, so omitting one clears it. `alert_id` and "
        "`run_label` are filled in when supplied and never cleared.\n"
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
    "HypothesisReport",
    "ReportAgentBriefingInput",
    "ReportAgentBriefingOutput",
    "ReportAgentRunInput",
    "ReportAgentRunOutput",
    "ReportableState",
    "StepReport",
    "report_agent_briefing",
    "report_agent_run",
]
