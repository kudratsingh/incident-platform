"""
`get_consumer_lag` — read the Redis-cached Kafka consumer lag.

Reads `kafka:consumer_lag:{group}` from Redis. The key convention
matches what the metrics loop writes for the platform's own
`worker-dispatcher` group; the eval seed script populates the same
key shape for the synthetic groups scenarios probe against.

Any group name is accepted. Unknown groups return `lag: null` rather
than erroring — the agent's LLM decides what "unknown" means for the
scenario at hand (defensive, matches the tool's original semantics).

Requires `telemetry:read`.
"""

from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from pydantic import BaseModel, ConfigDict, Field

# Prefix must match what the metrics loop (`_metrics_loop` in
# `app/workers/dispatcher.py`) and the eval seed script both use.
# Kept as a module constant so any three call sites stay aligned.
_CONSUMER_LAG_KEY_PREFIX = "kafka:consumer_lag:"


def _redis_key(group: str) -> str:
    return f"{_CONSUMER_LAG_KEY_PREFIX}{group}"


# Groups the eval seed script populates. Advertised in the input
# description so `tools/list` gives the agent a concrete menu.
# Passing a name outside this list is still allowed — it just returns
# `lag: null` if there's no Redis value for that key.
SEEDED_CONSUMER_GROUPS = (
    "worker-dispatcher",
    "billing-consumer",
    "orders-consumer",
    "notifications-consumer",
    "analytics-consumer",
    "payments-consumer",
    "shipping-consumer",
    "healthy-consumer",
)


class GetConsumerLagInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consumer_group: str = Field(
        default="worker-dispatcher",
        description=(
            "Kafka consumer group to inspect. Known groups: "
            + ", ".join(SEEDED_CONSUMER_GROUPS)
            + ". Any other name is accepted and returns lag: null when "
            "the platform has no cached value for it."
        ),
    )


class GetConsumerLagOutput(BaseModel):
    consumer_group: str
    lag: int | None = Field(
        description="Messages the group is behind, per the metrics loop's "
        "last emission. `null` when the cache "
        "is empty — typical for unknown groups or a Redis restart within "
        "the last ~60s."
    )
    cache_key: str = Field(
        description="Diagnostic — the Redis key the value was read from."
    )


@tool(
    "get_consumer_lag",
    description=(
        "Read the last-emitted Kafka consumer lag for one of the "
        "platform's consumer groups. Known consumer groups: "
        "worker-dispatcher, billing-consumer, orders-consumer, "
        "notifications-consumer, analytics-consumer, payments-consumer, "
        "shipping-consumer, healthy-consumer.\n"
        "FRESHNESS: cached in Redis, refreshed by a background loop "
        "every ~60s with a 90s TTL — never a live Kafka query. Measured "
        "behaviour: after a fault begins, the cached value catches up "
        "within ~60s; after a recovery it keeps reading the old high "
        "value for ~30s before dropping. Treat any single reading as up "
        "to a minute stale, in either direction.\n"
        "A STALLED CONSUMER STILL REPORTS LAG. A consumer that has "
        "stopped processing keeps its Kafka assignment, so this metric "
        "goes on reporting its real and climbing lag rather than going "
        "null — rising lag is the expected signal of a stall, not "
        "absence of data.\n"
        "NULL means the platform could not determine lag (consumer not "
        "started, no partition assignment, or the query errored). It is "
        "deliberately not reported as 0, because a fabricated 0 would "
        "read as healthy. Null is evidence of an unhealthy or "
        "restarting consumer, but it is not the usual symptom of a "
        "stall, and it carries no lag magnitude.\n"
        "Neither a number nor a null proves liveness. Consumer-group "
        "membership is the authoritative check — a group holds its "
        "assignment and keeps reporting accurate lag for minutes after "
        "its consumer stops, without being evicted."
    ),
    input_model=GetConsumerLagInput,
    output_model=GetConsumerLagOutput,
    required_scope=Scope.TELEMETRY_READ,
)
async def get_consumer_lag(
    inp: GetConsumerLagInput, ctx: ToolContext
) -> GetConsumerLagOutput:
    key = _redis_key(inp.consumer_group)
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


__all__ = [
    "SEEDED_CONSUMER_GROUPS",
    "GetConsumerLagInput",
    "GetConsumerLagOutput",
    "get_consumer_lag",
]
