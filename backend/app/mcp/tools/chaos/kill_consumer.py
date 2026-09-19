"""`kill_consumer` — shut down one Kafka consumer group.

Sets `chaos:kill:<group_id>` with a TTL; every consumer loop checks it at the
top of each poll and exits cleanly, then the supervisor decides about a
restart. With `sticky=true` a second key re-arms that flag for the rest of an
absolute window, so a `restart_consumer_group` cannot bring the group back
(ADR 0032). Requires `chaos:invoke` and `CHAOS_ENABLED=true`.
"""

from datetime import UTC, datetime, timedelta

from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext
from app.workers.kafka_consumer import (
    KILL_FLAG_VALUE,
    kill_key_for,
    sticky_kill_key_for,
)
from pydantic import BaseModel, ConfigDict, Field


class KillConsumerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consumer_group: str = Field(
        min_length=1,
        max_length=128,
        description="Kafka consumer group id to shut down. Examples: "
        "'worker-dispatcher', 'audit-writer', 'event-log'.",
    )
    ttl_seconds: int = Field(
        default=300,
        ge=1,
        le=3600,
        description="How long the kill flag stays active. After this "
        "the consumer's next restart succeeds and a sticky kill stops "
        "re-arming. Default 5 minutes.",
    )
    sticky: bool = Field(
        default=False,
        description="Keep the group down THROUGH a restart_consumer_group "
        "call, for the remainder of `ttl_seconds`. Default false, which is "
        "the plain kill a single restart fixes.",
    )


class KillConsumerOutput(BaseModel):
    consumer_group: str
    kill_key: str
    ttl_seconds: int
    sticky: bool
    sticky_key: str | None = Field(
        default=None,
        description="The second key a sticky kill wrote, or null for a "
        "plain kill. Null means nothing will re-arm the flag.",
    )
    expires_at: datetime = Field(
        description="When this kill ends — ISO-8601, UTC, the platform's "
        "clock, measured from this call. Absolute: restarts do not move it."
    )
    accepted: bool = Field(
        description="True if the kill flag was set. This does not confirm "
        "the consumer actually stopped — that happens on its next poll "
        "iteration (typically within 500ms)."
    )


@chaos_tool(
    "kill_consumer",
    description=(
        "Shut down one Kafka consumer group by setting a Redis flag "
        "the consumer's poll loop checks each iteration. The consumer "
        "exits cleanly; the worker's supervisor decides whether to "
        "restart. Effect lasts for `ttl_seconds` (default 300).\n"
        "STICKY: with `sticky: true` the group also stays down through a "
        "restart_consumer_group call. That action deletes the kill flag and "
        "truthfully reports having cleared it, and the supervisor finds the "
        "flag re-armed before it restarts anything — so the group is still "
        "down and a verify step has to read that off the group itself, not "
        "off the action's reply. This is how a first remediation attempt is "
        "made to genuinely fail.\n"
        "THE WINDOW IS ABSOLUTE: it runs from this call on the platform's "
        "clock and `expires_at` names its end. Restarting the group any "
        "number of times does not extend it, and nothing re-arms the flag "
        "once it has passed. A second sticky call on the same group "
        "replaces the window instead of adding to it, and clearing both "
        "keys ends it early."
    ),
    input_model=KillConsumerInput,
    output_model=KillConsumerOutput,
    blast_radius=BlastRadius.SINGLE_CONSUMER,
)
async def kill_consumer(
    inp: KillConsumerInput, ctx: ToolContext
) -> KillConsumerOutput:
    key = kill_key_for(inp.consumer_group)
    expires_at = datetime.now(UTC) + timedelta(seconds=inp.ttl_seconds)
    await ctx.redis.set(key, KILL_FLAG_VALUE, ex=inp.ttl_seconds)

    sticky_key: str | None = None
    if inp.sticky:
        # Second, deliberately: a marker written before a kill flag that never landed would hold
        # the group down with nothing to show for it. This order degrades to a plain kill.
        sticky_key = sticky_kill_key_for(inp.consumer_group)
        await ctx.redis.set(
            sticky_key, f"{expires_at.timestamp():.3f}", ex=inp.ttl_seconds
        )

    return KillConsumerOutput(
        consumer_group=inp.consumer_group,
        kill_key=key,
        ttl_seconds=inp.ttl_seconds,
        sticky=inp.sticky,
        sticky_key=sticky_key,
        expires_at=expires_at,
        accepted=True,
    )
