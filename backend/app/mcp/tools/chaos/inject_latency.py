"""
`inject_latency` — slow down one Kafka consumer group.

Mechanism: Redis key `chaos:latency:<group_id>` holds an integer
number of milliseconds. `BaseKafkaConsumer.run()` reads it at the top
of every poll iteration and sleeps for the requested amount before
calling `getmany`. Effect self-cleans when the key TTL expires.

Companion to `kill_consumer` — `kill` stops the consumer completely,
`inject_latency` degrades it. Useful for practicing "the platform is
slow" scenarios where the agent has to distinguish real degradation
from full failure.

Requires `chaos:invoke`. Registered only when `CHAOS_ENABLED=true`.
"""

from app.core.logging import get_logger
from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext
from app.workers.kafka_consumer import latency_key_for
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)


class InjectLatencyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consumer_group: str = Field(
        min_length=1,
        max_length=128,
        description="Consumer group to slow down.",
    )
    latency_ms: int = Field(
        ge=1,
        le=60_000,
        description="Milliseconds to sleep before every poll iteration. "
        "Practical range 100 (barely noticeable) to 5000 (obvious "
        "backpressure). Cap at 60000 keeps runaway latency from causing "
        "Kafka session-timeout kicks — see kafka_session_timeout_ms.",
    )
    ttl_seconds: int = Field(
        default=300,
        ge=1,
        le=3600,
        description="How long the injected latency stays active. Default "
        "5 minutes.",
    )


class InjectLatencyOutput(BaseModel):
    consumer_group: str
    latency_key: str
    latency_ms: int
    ttl_seconds: int
    accepted: bool


@chaos_tool(
    "inject_latency",
    description=(
        "Force one consumer group to sleep `latency_ms` before each "
        "Kafka poll. Effect self-cleans when the TTL expires. Watch "
        "`get_consumer_lag` — the group's lag will grow until the "
        "latency is removed."
    ),
    input_model=InjectLatencyInput,
    output_model=InjectLatencyOutput,
    blast_radius=BlastRadius.SINGLE_CONSUMER,
)
async def inject_latency(
    inp: InjectLatencyInput, ctx: ToolContext
) -> InjectLatencyOutput:
    key = latency_key_for(inp.consumer_group)
    await ctx.redis.set(key, str(inp.latency_ms), ex=inp.ttl_seconds)
    logger.warning(
        "chaos inject_latency set",
        extra={
            "group": inp.consumer_group,
            "latency_ms": inp.latency_ms,
            "ttl_seconds": inp.ttl_seconds,
        },
    )
    return InjectLatencyOutput(
        consumer_group=inp.consumer_group,
        latency_key=key,
        latency_ms=inp.latency_ms,
        ttl_seconds=inp.ttl_seconds,
        accepted=True,
    )
