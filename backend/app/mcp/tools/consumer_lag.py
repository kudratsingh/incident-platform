"""
`get_consumer_lag` — the first tool the incident-commander agent calls.

Reads the Redis-cached lag value the platform's metrics loop populates
every ~60s (`kafka:consumer_lag:worker-dispatcher`). Doesn't touch
Kafka — that would add latency and defeat the point of the cache. If
the cache is empty or expired we return `null` and let the agent decide
what "unknown" means.

Requires `telemetry:read`. First real exercise of the whole pipeline:
scope check → tool dispatch → service-layer call → audit row.
"""

from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.utils.backpressure import BACKPRESSURE_LAG_KEY
from pydantic import BaseModel, ConfigDict, Field


class GetConsumerLagInput(BaseModel):
    """No arguments today. The `consumer_group` field is a forward hook
    — once we expose more groups than `worker-dispatcher` (Wave 2), the
    agent will pass a specific group name. Defaulting to
    `worker-dispatcher` preserves the read shape until then."""

    model_config = ConfigDict(extra="forbid")

    consumer_group: str = Field(
        default="worker-dispatcher",
        description="Kafka consumer group to inspect. Only "
        "'worker-dispatcher' is exposed today.",
    )


class GetConsumerLagOutput(BaseModel):
    consumer_group: str
    lag: int | None = Field(
        description="Messages the group is behind, per the metrics loop's "
        "last emission. `null` means the cache is empty or expired — "
        "typically <60s after worker startup or a Redis restart."
    )
    cache_key: str = Field(
        description="Diagnostic — the Redis key the value was read from."
    )


# Only one group exposed today; the map exists so Wave 2 can add
# `event-log`, `read-model`, etc. by adding a row.
_KEY_BY_GROUP = {
    "worker-dispatcher": BACKPRESSURE_LAG_KEY,
}


@tool(
    "get_consumer_lag",
    description=(
        "Read the last-emitted Kafka consumer lag for one of the platform's "
        "consumer groups. Values come from the metrics loop's Redis cache — "
        "no live Kafka query. Returns null when the cache is empty."
    ),
    input_model=GetConsumerLagInput,
    output_model=GetConsumerLagOutput,
    required_scope=Scope.TELEMETRY_READ,
)
async def get_consumer_lag(
    inp: GetConsumerLagInput, ctx: ToolContext
) -> GetConsumerLagOutput:
    key = _KEY_BY_GROUP.get(inp.consumer_group)
    if key is None:
        # Unknown group — reveal exactly what we support so the agent's
        # LLM can self-correct on the next call.
        return GetConsumerLagOutput(
            consumer_group=inp.consumer_group,
            lag=None,
            cache_key=f"(no cache key for group '{inp.consumer_group}')",
        )

    raw = await ctx.redis.get(key)
    lag: int | None
    if raw is None:
        lag = None
    else:
        try:
            lag = int(raw)
        except (TypeError, ValueError):
            lag = None

    return GetConsumerLagOutput(
        consumer_group=inp.consumer_group,
        lag=lag,
        cache_key=key,
    )
