"""
`get_consumer_lag` — read the Redis-cached Kafka consumer lag.

Reads `kafka:consumer_lag:{group}` from Redis. The key convention
matches what the metrics loop writes for the platform's own
`worker-dispatcher` group; the eval seed script populates the same
key shape for the synthetic groups scenarios probe against.

Any group name is accepted. Unknown groups return `lag: null` rather
than erroring — the agent's LLM decides what "unknown" means for the
scenario at hand (defensive, matches the tool's original semantics).

Two things this tool is careful to say out loud (R2-17), because its
only consumer is an agent that cannot read `docs/REDIS.md`:

  - **Unknown is not zero.** `lag: 0` means measured-and-drained;
    `lag: null` means could-not-determine. They lead to opposite
    conclusions, so `lag_known` carries the distinction as its own
    boolean rather than leaving it implicit in a null. This is the same
    stance the dispatcher takes when it declines to emit a fabricated 0
    for the `ConsumerLag` metric, and that the CloudWatch backlog alarm
    documents as absent-datapoints-are-not-healthy.
  - **Only one group is live.** The FRESHNESS contract (~60s refresh,
    90s TTL) is true of `worker-dispatcher` alone. The other seven
    advertised groups are static fixtures written by
    `scripts/seed_eval_fixtures.py` and refreshed by nothing, so their
    value does not move while a fault runs. `source` says which kind of
    group answered, so "watch the lag grow" is never inferred for a
    group whose number cannot grow.

Requires `telemetry:read`.
"""

from typing import Literal

from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from pydantic import BaseModel, ConfigDict, Field

# Prefix must match what the metrics loop (`_metrics_loop` in
# `app/workers/dispatcher.py`) and the eval seed script both use.
# Kept as a module constant so any three call sites stay aligned.
_CONSUMER_LAG_KEY_PREFIX = "kafka:consumer_lag:"


def _redis_key(group: str) -> str:
    return f"{_CONSUMER_LAG_KEY_PREFIX}{group}"


# The single group whose lag is genuinely refreshed: the metrics loop
# writes it every ~60s with a 90s TTL, so it is fresh-or-absent and it
# moves while a fault is running.
LIVE_REFRESHED_GROUP = "worker-dispatcher"

# Groups whose lag is a recorded constant: written once by
# `seed_eval_fixtures._seed_consumer_lag` (durably, since R2-17) and
# re-anchored by the reset. Nothing refreshes them, so the value does
# not move. Mirror of that script's `_CONSUMER_LAGS` keys.
#
# Named for what the WIRE calls them ("static"), not for what they are
# internally, so the two vocabularies cannot drift: ADR 0012 rule 1
# bans lab words from the non-chaos tool surface, and
# `test_no_lab_vocabulary_on_non_chaos_tool_surface` enforces it. The
# operational truth an agent needs is "this number does not move",
# which is sayable without naming the lab.
STATIC_LAG_GROUPS = (
    "billing-consumer",
    "orders-consumer",
    "notifications-consumer",
    "analytics-consumer",
    "payments-consumer",
    "shipping-consumer",
    "healthy-consumer",
)

# Groups the eval seed script populates. Advertised in the input
# description so `tools/list` gives the agent a concrete menu.
# Passing a name outside this list is still allowed — it just returns
# `lag: null` if there's no Redis value for that key.
SEEDED_CONSUMER_GROUPS = (LIVE_REFRESHED_GROUP,) + STATIC_LAG_GROUPS

LagSource = Literal["live", "static", "unrecognized"]


def _source_for(group: str) -> LagSource:
    if group == LIVE_REFRESHED_GROUP:
        return "live"
    if group in STATIC_LAG_GROUPS:
        return "static"
    return "unrecognized"


class GetConsumerLagInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consumer_group: str = Field(
        default=LIVE_REFRESHED_GROUP,
        description=(
            "Kafka consumer group to inspect. Continuously refreshed "
            f"group: {LIVE_REFRESHED_GROUP}. Groups reported from a "
            "recorded constant: "
            + ", ".join(STATIC_LAG_GROUPS)
            + ". Any other name is accepted and returns lag: null with "
            "source: unrecognized."
        ),
    )


class GetConsumerLagOutput(BaseModel):
    consumer_group: str
    lag: int | None = Field(
        description="Messages the group is behind. `null` when the "
        "platform could not determine lag — NOT a synonym for 0. Check "
        "`lag_known` before comparing this against a threshold."
    )
    lag_known: bool = Field(
        description="True when `lag` is a real measurement, including a "
        "measured 0 (group caught up — healthy). False when the platform "
        "has no value for the group: nothing recorded, a group it does "
        "not track, or a consumer that could not report. A false here is "
        "evidence of missing information, never of a healthy queue."
    )
    source: LagSource = Field(
        description="Where the number comes from, which determines "
        "whether it can change. `live` — refreshed every ~60s (90s TTL); "
        "it moves as conditions do. `static` — a recorded constant; "
        "re-reading returns the same number, so lag growth or drain "
        "cannot be observed on this group. `unrecognized` — not a group "
        "this platform tracks; check the spelling before concluding "
        "anything from the null."
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
        "FRESHNESS: cached in Redis, never a live Kafka query, and the "
        "refresh behaviour differs per group — read `source` on the "
        "response before reasoning about change over time.\n"
        "  - `worker-dispatcher` (source: live) is refreshed by a "
        "background loop every ~60s with a 90s TTL. Measured behaviour: "
        "after a fault begins the cached value catches up within ~60s; "
        "after a recovery it keeps reading the old high value for ~30s "
        "before dropping. Treat any single reading as up to a minute "
        "stale, in either direction. This is the only group whose lag "
        "moves, and so the only one where watching lag grow or drain is "
        "a valid way to observe a change.\n"
        "  - The other seven (source: static) report a recorded "
        "constant that nothing refreshes. Re-reading one after an "
        "action returns the same number — that is the expected "
        "behaviour, not evidence the action failed, and lag on these "
        "groups will never be seen to grow.\n"
        "A STALLED CONSUMER STILL REPORTS LAG. A consumer that has "
        "stopped processing keeps its Kafka assignment, so this metric "
        "goes on reporting its real and climbing lag rather than going "
        "null — rising lag is the expected signal of a stall, not "
        "absence of data.\n"
        "UNKNOWN IS NOT ZERO. `lag_known: false` with `lag: null` means "
        "the platform could not determine lag (nothing recorded for the "
        "group, unknown group, consumer not started, no partition "
        "assignment, or the query errored). It is deliberately not "
        "reported as 0, because "
        "a fabricated 0 would read as healthy. A measured `lag: 0` with "
        "`lag_known: true` is the opposite finding — the group is "
        "caught up. Never treat a null as a zero.\n"
        "`source: static` with `lag_known: false` means a value that "
        "should be recorded for this group is absent — an environment "
        "problem to report, not a fault to diagnose.\n"
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
        # Derived from the read, not from the group name: a tracked
        # group with a missing fixture is just as unknown as an
        # untracked one, and `source` is what tells those apart.
        lag_known=lag is not None,
        source=_source_for(inp.consumer_group),
        cache_key=key,
    )


__all__ = [
    "STATIC_LAG_GROUPS",
    "LIVE_REFRESHED_GROUP",
    "SEEDED_CONSUMER_GROUPS",
    "GetConsumerLagInput",
    "GetConsumerLagOutput",
    "get_consumer_lag",
]
