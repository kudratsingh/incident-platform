"""`pause_control_loop` — stop one of the worker's eleven background loops.

Sets `chaos:pause:<loop>`, which each loop reads once per iteration before
skipping its work (`app/workers/control_loop_pause.py`, ADR 0027); teardown is
the TTL plus the reset's `chaos:*` sweep. Unlike `kill_consumer`'s open group
id, `loop_name` is a closed enum, pinned by `test_pause_control_loop.py`.
"""

from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext
from app.workers.control_loop_pause import (
    ControlLoopName,
    pause_key_for,
    tick_interval_seconds,
)
from pydantic import BaseModel, ConfigDict, Field


class PauseControlLoopInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loop_name: ControlLoopName = Field(
        description=(
            "Which background loop to pause. Closed set — anything else is "
            "rejected as an invalid argument. The eight Kafka consumer groups "
            "are not here: use kill_consumer for those."
        ),
    )
    ttl_seconds: int = Field(
        default=300,
        ge=1,
        le=3600,
        description=(
            "How long the pause flag stays set. The loop resumes by itself on "
            "the first iteration after it expires — nothing has to be called. "
            "Default 5 minutes."
        ),
    )


class PauseControlLoopOutput(BaseModel):
    loop_name: ControlLoopName
    pause_key: str
    ttl_seconds: int
    tick_interval_seconds: float | None = Field(
        description=(
            "Nominal seconds between this loop's iterations, as configured "
            "right now, or null when the loop is not iterating at all "
            "(SLO evaluation with its interval set to 0). The pause takes "
            "effect on the loop's next iteration, so a ttl_seconds smaller "
            "than this expires before the loop ever reads the flag."
        )
    )
    accepted: bool = Field(
        description=(
            "True if the pause flag was set. This does not confirm the loop "
            "has stopped — that happens on its next iteration, at most "
            "tick_interval_seconds away."
        )
    )


@chaos_tool(
    "pause_control_loop",
    description=(
        "Pause one of the worker's background loops by setting a Redis flag "
        "the loop reads once per iteration; it does no work while the flag is "
        "set and resumes by itself when the flag expires after `ttl_seconds` "
        "(default 300). One loop only: the worker process, its Kafka consumer "
        "groups and the other loops are unaffected. `loop_name` is a closed "
        "set; `tick_interval_seconds` in the result says how long the loop "
        "takes to notice. Use `kill_consumer` for a Kafka consumer group."
    ),
    input_model=PauseControlLoopInput,
    output_model=PauseControlLoopOutput,
    blast_radius=BlastRadius.SINGLE_LOOP,
)
async def pause_control_loop(
    inp: PauseControlLoopInput, ctx: ToolContext
) -> PauseControlLoopOutput:
    key = pause_key_for(inp.loop_name)
    await ctx.redis.set(key, "paused", ex=inp.ttl_seconds)
    return PauseControlLoopOutput(
        loop_name=inp.loop_name,
        pause_key=key,
        ttl_seconds=inp.ttl_seconds,
        tick_interval_seconds=tick_interval_seconds(inp.loop_name),
        accepted=True,
    )
