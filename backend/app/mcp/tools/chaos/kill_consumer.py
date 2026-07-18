"""
`kill_consumer` — chaos tool that shuts down one Kafka consumer group.

Mechanism: set a Redis key `chaos:kill:<group_id>` with a TTL. Every
consumer loop checks the key at the top of each poll iteration and
exits cleanly when it appears. The worker's supervisor then decides
whether to restart.

Kept intentionally simple — no live Kafka introspection, no consumer
group lookup, no strong verification. The agent inspects `tools/list`
to see it's available and calls it with a group name; the platform
either has that group or doesn't.

Requires `chaos:invoke`. Registered only when `CHAOS_ENABLED=true`
(see `app/mcp/chaos.py`).
"""

from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext
from app.workers.kafka_consumer import kill_key_for
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
        "the consumer's next restart succeeds. Default 5 minutes.",
    )


class KillConsumerOutput(BaseModel):
    consumer_group: str
    kill_key: str
    ttl_seconds: int
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
        "restart. Effect lasts for `ttl_seconds` (default 300)."
    ),
    input_model=KillConsumerInput,
    output_model=KillConsumerOutput,
    blast_radius=BlastRadius.SINGLE_CONSUMER,
)
async def kill_consumer(
    inp: KillConsumerInput, ctx: ToolContext
) -> KillConsumerOutput:
    key = kill_key_for(inp.consumer_group)
    await ctx.redis.set(key, "killed", ex=inp.ttl_seconds)
    return KillConsumerOutput(
        consumer_group=inp.consumer_group,
        kill_key=key,
        ttl_seconds=inp.ttl_seconds,
        accepted=True,
    )
