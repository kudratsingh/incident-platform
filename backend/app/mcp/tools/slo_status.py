"""
`get_slo_status` — whether an objective is really being missed.

The computation already existed and had exactly one caller, an admin REST route, so an alert
saying latency was above target could be neither confirmed nor refuted from the tool surface
(WO-R3-217, plan 01 §6). This surfaces `services/slo.compute_all` unchanged: same objectives,
same windows, same arithmetic. Needs `telemetry:read`.
"""

from datetime import UTC, datetime

from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.services.slo import (
    FAST_BURN_THRESHOLD,
    SLOState,
    compute_all,
    is_fast_burning,
)
from pydantic import BaseModel, ConfigDict, Field


class GetSloStatusInput(BaseModel):
    # No fields and `extra="forbid"`: the description promises no filtering or paging.
    model_config = ConfigDict(extra="forbid")


class SloObjective(BaseModel):
    id: str = Field(
        description="Stable identifier for this objective. Fixed by the platform; the "
        "set of objectives does not change between calls."
    )
    name: str = Field(description="What the objective is called, in plain words.")
    description: str = Field(
        description="What this objective measures and what it deliberately leaves out "
        "of the fraction."
    )
    target: float = Field(
        description="The share of work that must succeed, as a fraction in [0, 1] — "
        "0.99 is 99%. The objective is missed below this, not at it."
    )
    window_hours: int = Field(
        description="How many hours back the counts below cover. Everything older than "
        "this is outside the measurement, however bad it was."
    )
    total: int = Field(
        description="How much settled work is in the window: the denominator. **0 means "
        "nothing settled in the window**, which makes every number beside it an absence "
        "of evidence rather than a healthy reading. Always read this first."
    )
    failed: int = Field(
        description="How much of `total` counts as a failure for this objective: the "
        "numerator."
    )
    current_success_rate: float = Field(
        description="`1 - failed/total`, as a fraction in [0, 1]. 1.0 when `total` is 0, "
        "because there is nothing that failed — not because the platform is healthy."
    )
    budget_remaining_pct: float = Field(
        description="How much of the error budget this window has left, as a "
        "percentage. 100 is untouched; 0 is exactly spent; negative means overspent, "
        "and it is clamped at -100 rather than reporting how far past it went."
    )
    burn_rate: float | None = Field(
        default=None,
        description="How many times faster than sustainable the budget is being spent. "
        "1.0 spends the window's budget exactly over the window. `null` means the rate "
        "is unbounded — an objective that admits no failures had one — and is not "
        "unknown; it is above every threshold.",
    )
    healthy: bool = Field(
        description="Whether `current_success_rate` is at or above `target` over this "
        "window. True with `total: 0`, for the reason that field gives."
    )
    fast_burn: bool = Field(
        description="Whether `burn_rate` is at or above `fast_burn_threshold` — the rate "
        "at which this platform raises an alert rather than merely recording a miss."
    )


class GetSloStatusOutput(BaseModel):
    measured_at: datetime = Field(
        description="When this reading was taken — ISO-8601, UTC, the clock of the "
        "process that answered the call. Each objective's window ends here and begins "
        "`window_hours` before it."
    )
    objectives: tuple[SloObjective, ...] = Field(
        description="Every objective this platform declares, in the order it declares "
        "them. Not a page: there is no cap and nothing is omitted."
    )
    total: int = Field(
        description="How many objectives are in `objectives`. Equal to its length "
        "always — it is a count of what the platform declares, not of what was returned."
    )
    fast_burn_threshold: float = Field(
        description="The burn rate at which this platform raises an alert. The yardstick "
        "every `burn_rate` above is read against."
    )


def _objective(state: SLOState) -> SloObjective:
    """One computed objective. An unbounded rate is rendered null — JSON has no
    spelling for an infinity, and the field says null means unbounded, not unknown."""
    rate = state.burn_rate
    return SloObjective(
        id=state.definition.id,
        name=state.definition.name,
        description=state.definition.description,
        target=state.definition.target,
        window_hours=state.definition.window_hours,
        total=state.total,
        failed=state.failed,
        current_success_rate=state.current,
        budget_remaining_pct=state.budget_remaining_pct,
        burn_rate=None if rate == float("inf") else rate,
        healthy=state.healthy,
        fast_burn=is_fast_burning(state),
    )


@tool(
    "get_slo_status",
    description=(
        "Read how every service-level objective this platform declares is "
        "doing: how much settled work is in its window, how much of it failed, "
        "how much error budget is left and how fast that budget is being "
        "spent. This is the reading that confirms or refutes a claim that an "
        "objective is being missed.\n"
        "FRESHNESS AND WHICH CLOCK. Nothing is cached. Every objective is "
        "computed by query at call time over its own window, which ends at "
        "`measured_at` — the clock of the process that answered the call — and "
        "begins `window_hours` before it. A second call a few seconds later is "
        "genuine new evidence, though on a 24-hour window a few seconds move "
        "the numbers very little.\n"
        "NO PAGING, NOTHING CAPPED. It takes no arguments. There is no `limit`, "
        "no `offset` and no filter: every declared objective comes back every "
        "time, and `total` is the count of them rather than a page size.\n"
        "SCOPE. The counts cover the work your own tenant can see. The "
        "platform's own periodic check, the one that raises an alert, runs "
        "across every tenant — so a healthy reading here does not prove no "
        "alert was justified, and an unhealthy one is about your tenant "
        "specifically.\n"
        "READ `total` FIRST, ALWAYS. With `total: 0` nothing settled in the "
        "window, so `budget_remaining_pct` is 100 and `healthy` is true because "
        "there is nothing that failed — not because anything is working. That "
        "is the reading a quiet platform gives and the one most easily "
        "misread: no traffic is an absence of evidence, not evidence of health.\n"
        "BREACH IS NOT THE SAME AS A PAGE. `healthy: false` means the window is "
        "below target. `fast_burn: true` means the budget is being spent at or "
        "above `fast_burn_threshold`, which is the rate at which this platform "
        "raises an alert. An objective can be missed for a window without "
        "burning fast, and can burn fast for minutes while the window still "
        "looks acceptable.\n"
        "WHAT THIS CANNOT SEE. It reports the objectives as declared and "
        "nothing else: no per-endpoint latency, no breakdown by job type, and "
        "no history — each call is one window ending now, so it cannot show "
        "when a burn started. Whether an alert exists is a separate reading."
    ),
    input_model=GetSloStatusInput,
    output_model=GetSloStatusOutput,
    required_scope=Scope.TELEMETRY_READ,
)
async def get_slo_status(
    _inp: GetSloStatusInput, ctx: ToolContext
) -> GetSloStatusOutput:
    states = await compute_all(ctx.db)
    objectives = tuple(_objective(state) for state in states)
    return GetSloStatusOutput(
        measured_at=datetime.now(UTC),
        objectives=objectives,
        total=len(objectives),
        fast_burn_threshold=FAST_BURN_THRESHOLD,
    )


__all__ = [
    "GetSloStatusInput",
    "GetSloStatusOutput",
    "SloObjective",
    "get_slo_status",
]
