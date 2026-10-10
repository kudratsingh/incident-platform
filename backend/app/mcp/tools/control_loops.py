"""
`get_control_loops` — whether each of the worker's background loops is running.

The worker runs eleven timed loops beside its Kafka consumers (ADR 0027), and when one stops
the work it moves stops moving, usually with nothing dead-lettered and no queue to watch. The
resume sweep is the sharp case: it is the backstop that promotes a `waiting` child whose parents
have all completed, and a stranded chain with that sweep held and the resolver dead was
unreadable from every tool the agent had (INC-008, WO-R3-371, ADR 0041).

One reading per loop: paused or not (and for how much longer), when it last completed a pass,
and how often it is meant to run. Nothing says why a loop is held or who held it — the state is
the system under test, the actor is the lab, and the lab stays invisible (ADR 0012). The pause
predicate is `loop_is_paused`'s own (`control_loop_pause.loop_pause_state`); the pass record is
`core/loop_heartbeat.py`, and the relay's is its ADR 0028 stamp. Needs `telemetry:read`.
"""

from datetime import UTC, datetime

from app.core.logging import get_logger
from app.core.loop_heartbeat import read_loop_run
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.workers.control_loop_pause import (
    ControlLoopName,
    loop_pause_state,
    tick_interval_seconds,
)
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)

#: Why nothing could be read, in the caller's words. The one unknown this tool reports as a
#: whole: per-loop absence ("no pass on record") is a reading, not an outage.
STORE_UNREACHABLE = (
    "the platform could not read the store that holds the loops' state, so whether "
    "any loop is paused and when each last ran are unknown"
)


class GetControlLoopsInput(BaseModel):
    # No fields and `extra="forbid"`: the description promises every loop, no filter, no paging.
    model_config = ConfigDict(extra="forbid")


# No class docstrings on the two models below: Pydantic copies one into the schema the
# commander pins (plat #210). The field descriptions carry the meaning instead.
class ControlLoopState(BaseModel):
    name: ControlLoopName = Field(
        description="Which loop. A fixed platform name, the same across calls and "
        "restarts; the tool description says what each one does."
    )
    paused: bool | None = Field(
        description="True while this loop is set to skip its work. It resumes by "
        "itself when `paused_expires_in_seconds` runs out, and nothing on this "
        "surface lifts it sooner. False when it is not held. `null` only when "
        "`unknown_reason` is set — then whether it is held is unknown, which is "
        "not the same as false."
    )
    paused_expires_in_seconds: int | None = Field(
        default=None,
        description="Seconds until the pause lifts and the loop resumes on its own. "
        "`null` when the loop is not paused, when the pause carries no expiry, and "
        "when the state is unknown.",
    )
    tick_interval_seconds: float | None = Field(
        default=None,
        description="How often this loop is configured to run on this deployment, "
        "in seconds — the yardstick for `last_run_age_seconds`. `null` when the "
        "loop is configured not to run at all.",
    )
    last_run_at: datetime | None = Field(
        default=None,
        description="When this loop last completed a pass — ISO-8601, UTC, recorded "
        "by the worker process on its clock at the end of the pass. A pass counts "
        "whether or not there was work to do, so an idle loop stays fresh here; a "
        "paused loop does not record passes. `null` when no completed pass is on "
        "record: none since the platform started (a loop with a long interval, "
        "like `digest`, reads null for most of its first interval), the last one "
        "is older than the platform keeps or the record was lost, or the state "
        "is unknown.",
    )
    last_run_age_seconds: float | None = Field(
        default=None,
        description="How long ago `last_run_at` was, in seconds, against "
        "`measured_at`. `null` exactly when `last_run_at` is null. It compares "
        "two processes' clocks, so treat a second either way as noise, and a "
        "recorded time later than `measured_at` reads 0, never negative.",
    )


class GetControlLoopsOutput(BaseModel):
    measured_at: datetime = Field(
        description="When this reading was taken — ISO-8601, UTC, the clock of the "
        "process that answered this call. Every `last_run_age_seconds` is this time "
        "minus the `last_run_at` beside it."
    )
    loops: list[ControlLoopState] = Field(
        description="Every background loop, always all of them, in the same order "
        "on every call."
    )
    total: int = Field(
        description="How many loops are listed. A count of all of them, not a page "
        "size: nothing is ever left out."
    )
    unknown_reason: str | None = Field(
        default=None,
        description="Set only when the platform could not read the loops' state at "
        "all; every `paused`, `last_run_at` and `last_run_age_seconds` is then null. "
        "That is missing information, never eleven healthy loops. `null` on a "
        "normal reading.",
    )


def _age_seconds(measured_at: datetime, at: datetime | None) -> float | None:
    """Seconds between a recorded pass and the reading, clamped at 0 for clock skew."""
    if at is None:
        return None
    return round(max(0.0, (measured_at - at).total_seconds()), 1)


async def _state(
    ctx: ToolContext, loop: ControlLoopName, measured_at: datetime
) -> ControlLoopState:
    paused, expires_in = await loop_pause_state(ctx.redis, loop)
    last_run_at = await read_loop_run(ctx.redis, loop)
    return ControlLoopState(
        name=loop,
        paused=paused,
        paused_expires_in_seconds=expires_in,
        tick_interval_seconds=tick_interval_seconds(loop),
        last_run_at=last_run_at,
        last_run_age_seconds=_age_seconds(measured_at, last_run_at),
    )


@tool(
    "get_control_loops",
    description=(
        "Read whether each of the worker's background loops is running: for "
        "every loop, whether it is paused (and for how much longer), when it last "
        "completed a pass, and how often it is meant to run.\n"
        "WHAT THEY ARE. Besides its Kafka consumer groups (read those with "
        "`get_consumer_lag`), the worker runs timed loops that move work along "
        "without waiting for an event. When one stops, the work it moves stops "
        "moving, usually with no error and nothing dead-lettered. The loops:\n"
        "  - `outbox_relay` — publishes committed job state changes to Kafka "
        "(`get_outbox_status` reads the queue it drains).\n"
        "  - `delayed_retry_promote` — re-submits a failed job once its retry "
        "backoff has elapsed.\n"
        "  - `dlq_replay_promote` — runs dead-letter replays that were scheduled "
        "for later.\n"
        "  - `resume_unblocked_waiting` — moves a `waiting` job whose parent jobs "
        "have all completed on to run. It backstops the `dependency-resolver` "
        "consumer group, which does the same when a parent's completion arrives: "
        "if that promotion is missed, or the resolver is not consuming, this loop "
        "is what eventually moves the child. With both stopped, a child of a "
        "completed parent stays `waiting` indefinitely.\n"
        "  - `stale_pending_backstop` — re-submits a `pending` job that nothing "
        "is going to pick up.\n"
        "  - `stale_running_sweep` — dead-letters a `running` job whose worker "
        "died.\n"
        "  - `lease_renewal` — keeps the `running` jobs a worker holds marked as "
        "alive.\n"
        "  - `slo_evaluation` — evaluates the service-level objectives and raises "
        "a fast-burn alert.\n"
        "  - `metrics` — measures consumer lag (what `get_consumer_lag` reports) "
        "and evaluates the platform's own alert rules.\n"
        "  - `digest` — writes the periodic incident summary.\n"
        "  - `idempotency_reaper` — deletes expired idempotency records.\n"
        "READING ONE. `tick_interval_seconds` is how often the loop is meant to "
        "run, and the yardstick for `last_run_age_seconds`: an age within a few "
        "multiples of the interval is a loop that is running; an age many times "
        "the interval is a loop that has not completed a pass in that long. "
        "`paused: true` means the loop is set to skip its work until "
        "`paused_expires_in_seconds` runs out, when it resumes by itself; its "
        "`last_run_age_seconds` grows while it is held. A loop can also stop "
        "without being paused — a growing age with `paused: false` is a loop that "
        "is not completing passes for some other reason.\n"
        "WHAT IT DOES NOT SAY. Why a loop is paused, who or what paused it, or "
        "why a pass did not complete. It reports state and times only.\n"
        "FRESHNESS AND WHICH CLOCK. Nothing is cached: the pause state is read at "
        "call time, so a second call is genuine new evidence. `last_run_at` is "
        "written by the worker process on its clock at the end of each pass; "
        "`measured_at` is the clock of the process answering this call, and each "
        "age compares the two — treat a second either way as noise.\n"
        "NO PAGING, NOTHING CAPPED. It takes no arguments and always returns every "
        "loop, in the same order; `total` is that count, not a page size.\n"
        "UNKNOWN IS NULL. `last_run_at: null` means no completed pass is on "
        "record — none since the platform started, or the last one is older than "
        "the platform keeps or its record was lost. When the platform cannot "
        "read the loops' state at all, `unknown_reason` says so and every "
        "`paused` and `last_run_at` is null: that is missing information, not "
        "healthy loops.\n"
        "SCOPE. The loops are platform-wide and serve every tenant; this reading "
        "is not scoped to yours."
    ),
    input_model=GetControlLoopsInput,
    output_model=GetControlLoopsOutput,
    required_scope=Scope.TELEMETRY_READ,
)
async def get_control_loops(
    _inp: GetControlLoopsInput, ctx: ToolContext
) -> GetControlLoopsOutput:
    measured_at = datetime.now(UTC)
    unknown_reason: str | None = None
    try:
        states = [await _state(ctx, loop, measured_at) for loop in ControlLoopName]
    except Exception as exc:
        # All-or-nothing on purpose: one store holds every record, so a failure is the whole
        # reading's, and a half-read listing would mix knowns with silent unknowns.
        logger.warning("control loop state unreadable", extra={"error": str(exc)})
        unknown_reason = STORE_UNREACHABLE
        states = [
            ControlLoopState(
                name=loop,
                paused=None,
                tick_interval_seconds=tick_interval_seconds(loop),
            )
            for loop in ControlLoopName
        ]
    return GetControlLoopsOutput(
        measured_at=measured_at,
        loops=states,
        total=len(states),
        unknown_reason=unknown_reason,
    )


__all__ = [
    "STORE_UNREACHABLE",
    "ControlLoopState",
    "GetControlLoopsInput",
    "GetControlLoopsOutput",
    "get_control_loops",
]
