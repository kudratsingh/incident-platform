"""
`get_consumer_lag` — the Redis-cached Kafka consumer lag from `kafka:consumer_lag:`.
Unknown groups return `lag: null`. Three things the description says out loud, since
the agent cannot read this file: unknown is not zero (`lag_known`), only
`worker-dispatcher`'s number moves (`source`), and one call carries the trend via
`measured_at` + `recent_samples` (R2-17, WO-R3-254). Requires `telemetry:read`.

The reading itself moved to `app/core/consumer_lag.py` (WO-R3-312) so the operator
console reports the same number from the same arithmetic; this module is the agent's
wording for it, and the wire shape is unchanged.
"""

from datetime import datetime

from app.core.consumer_lag import (
    LIVE_REFRESHED_GROUP,
    SEEDED_CONSUMER_GROUPS,
    STATIC_LAG_GROUPS,
    LagSource,
    parse_lag,
    read_lag,
)
from app.core.consumer_lag import (
    lag_key as _redis_key,
)
from app.core.consumer_lag import (
    samples_key as _samples_key,
)
from app.core.logging import get_logger
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)

# Re-exported under their old private names so the existing tests keep importing them
# from here; the definitions are in core.
_parse_lag = parse_lag


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


class LagSample(BaseModel):
    """One past measurement of the same group's lag."""

    lag: int = Field(
        description="Messages the group was behind when this sample was "
        "taken."
    )
    measured_at: datetime = Field(
        description="When this sample was measured — ISO-8601, UTC, the "
        "platform's clock."
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
        "whether it can change. `live` — re-measured by a background "
        "pass and cached under a TTL of a few passes; it moves as "
        "conditions do. `static` — a recorded constant; "
        "re-reading returns the same number, so lag growth or drain "
        "cannot be observed on this group. `unrecognized` — not a group "
        "this platform tracks; check the spelling before concluding "
        "anything from the null."
    )
    cache_key: str = Field(
        description="Diagnostic — the Redis key the value was read from."
    )
    measured_at: datetime | None = Field(
        default=None,
        description="When the `lag` above was measured — ISO-8601, UTC. "
        "`null` when the platform holds no measurement time for this "
        "number: a group reporting a recorded constant has none (it was "
        "never measured at a moment), and neither does a reading whose "
        "measurement time was not recorded or no longer matches the "
        "current value. Never a guess — a null here means unknown, and "
        "the number in `lag` is still the current one.",
    )
    age_seconds: int | None = Field(
        default=None,
        description="How long ago `measured_at` was, in whole seconds. "
        "`null` exactly when `measured_at` is null. This is the only "
        "honest way to judge freshness: the sampling interval is "
        "deployment-configured, so an age is a fact and an assumed "
        "interval is not. Compare it against the gaps between "
        "`recent_samples` to see how often this deployment measures.",
    )
    recent_samples: list[LagSample] = Field(
        default_factory=list,
        description="The measurements recorded for this group over the "
        "last 15 minutes, newest first, the current one included — one "
        "per measurement pass, so how many there are depends on how "
        "often this deployment samples. "
        "Comparing them is how to tell "
        "a climbing lag from a flat one without waiting. Empty for a "
        "group reporting a recorded constant (nothing measures it), and "
        "empty for the continuously-refreshed group when no window has "
        "been recorded yet — an empty list is absence of history, never "
        "evidence the lag is steady.",
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
        "  - `worker-dispatcher` (source: live) is re-measured by a "
        "background pass whose interval is deployment-configured, and "
        "cached under a TTL of a few passes. THE INTERVAL IS NOT A "
        "NUMBER YOU CAN ASSUME: read `age_seconds` for how old the "
        "reading in front of you is, and the gaps between "
        "`recent_samples` for how often this deployment measures. Any "
        "single reading is up to one interval stale in either direction "
        "— after a fault begins the cached value catches up within about "
        "one interval, and after a recovery it can keep reading the old "
        "high value for a fraction of one. This is the only group whose "
        "lag moves, and so the only one where watching lag grow or drain "
        "is a valid way to observe a change.\n"
        "  - The other seven (source: static) report a recorded "
        "constant that nothing refreshes. Re-reading one after an "
        "action returns the same number — that is the expected "
        "behaviour, not evidence the action failed, and lag on these "
        "groups will never be seen to grow.\n"
        "ONE CALL SHOWS THE TREND. Every response carries `measured_at` "
        "(when this number was measured), `age_seconds` (how old it is), "
        "and `recent_samples` — the measurements recorded over the last "
        "15 minutes, newest first, each with its own time, the current "
        "one included. Compare those "
        "samples to decide whether lag is climbing, draining or flat. "
        "That comparison is the evidence; a second call is not, because "
        "a new measurement exists only once a pass has taken one. Two "
        "calls inside one interval therefore return the SAME number with "
        "the SAME `measured_at`, and that repetition means 'not "
        "re-measured yet', never 'not moving' — reading sooner cannot "
        "show change that has not been measured. If you need a genuinely "
        "newer number than the one in front of you, it exists once "
        "`age_seconds` exceeds the gap between the newest two samples; "
        "until then this response already "
        "contains every reading the platform has. On the seven groups "
        "reporting a recorded constant, `measured_at` and `age_seconds` "
        "are null and `recent_samples` is empty: a constant was never "
        "measured at a moment, so it has no time and no history. "
        "`recent_samples` can also be empty for worker-dispatcher when "
        "nothing has been recorded yet — an empty list is missing "
        "history, not a flat line.\n"
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
    reading = await read_lag(ctx.redis, inp.consumer_group)
    return GetConsumerLagOutput(
        consumer_group=reading.consumer_group,
        lag=reading.lag,
        lag_known=reading.lag_known,
        source=reading.source,
        cache_key=reading.cache_key,
        measured_at=reading.measured_at,
        age_seconds=reading.age_seconds,
        recent_samples=[
            LagSample(lag=s.lag, measured_at=s.measured_at)
            for s in reading.recent_samples
        ],
    )


__all__ = [
    "STATIC_LAG_GROUPS",
    "LIVE_REFRESHED_GROUP",
    "SEEDED_CONSUMER_GROUPS",
    "GetConsumerLagInput",
    "GetConsumerLagOutput",
    "LagSample",
    # Re-exported under their pre-WO-R3-312 private names: several tests and
    # `restart_consumer_group` import them from here, and moving the definitions to
    # core is not a reason to move every import site. `_LAG_SAMPLES_KEEP` left with
    # WO-R3-338: the window is pruned by time, so a count of passes is no longer one of
    # this module's numbers.
    "_parse_lag",
    "_redis_key",
    "_samples_key",
    "get_consumer_lag",
]
