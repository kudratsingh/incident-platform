"""
`get_consumer_lag` — the Redis-cached Kafka consumer lag from `kafka:consumer_lag:`.
Unknown groups return `lag: null`. Four things the description says out loud, since
the agent cannot read this file: unknown is not zero (`lag_known`), only the two
measured groups' numbers move (`source`), one call carries the trend via
`measured_at` + `recent_samples` (R2-17, WO-R3-254), and a flat lag is not a live
consumer — `polling` is, the platform's verdict drawn from `last_poll_at` (WO-R3-371,
then WO-R3-373 when the bare number was misread; ADR 0041). Requires `telemetry:read`.

The reading itself moved to `app/core/consumer_lag.py` (WO-R3-312) so the operator
console reports the same number from the same arithmetic; this module is the agent's
wording for it, and the wire shape is unchanged.
"""

from datetime import datetime

from app.core.consumer_lag import (
    LAG_SAMPLES_AGENT_CAP,
    LIVE_REFRESHED_GROUP,
    LIVE_REFRESHED_GROUPS,
    POLLING_INTERVALS,
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
            "groups: " + ", ".join(LIVE_REFRESHED_GROUPS) + ". Groups reported from a "
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
        "last 15 minutes, newest first, the current one included — at "
        f"most {LAG_SAMPLES_AGENT_CAP} of them, one per measurement pass. A "
        "deployment that samples more often than that records more than "
        "this call returns; the ones returned are always the newest. "
        "Comparing them is how to tell "
        "a climbing lag from a flat one without waiting. Empty for a "
        "group reporting a recorded constant (nothing measures it), and "
        "empty for a continuously-refreshed group when no window has "
        "been recorded yet — an empty list is absence of history, never "
        "evidence the lag is steady.",
    )
    last_poll_at: datetime | None = Field(
        default=None,
        description="When this group's consumer last asked Kafka for "
        "messages — ISO-8601, UTC, the worker's clock. A running consumer "
        "asks every couple of seconds whether or not there is anything to "
        "read; `polling` is the verdict drawn from this time. `null` for a "
        "group reporting a recorded constant (nothing polls it) and whenever "
        "the platform holds no poll time for the group — unknown, never "
        "evidence either way.",
    )
    last_poll_age_seconds: int | None = Field(
        default=None,
        description="How long ago `last_poll_at` was, in whole seconds — "
        "the number behind `polling`. `null` exactly when `last_poll_at` is "
        "null.",
    )
    polling: bool | None = Field(
        default=None,
        description="Whether this group's consumer is asking Kafka for "
        "messages — the platform's verdict, so read this rather than "
        "comparing ages yourself. `true`: when the platform last measured, "
        "the consumer had polled within the last "
        f"{POLLING_INTERVALS} poll intervals (`poll_interval_seconds`). "
        "`false`: it had not — the consumer has stopped (dead, crashed or "
        "wedged), whatever `lag` reads, a lag of 0 included. `null`: the "
        "platform holds no poll time for this group (a group reporting a "
        "recorded constant, an unknown group, or nothing recorded yet) — "
        "unknown, never evidence either way.",
    )
    poll_interval_seconds: float | None = Field(
        default=None,
        description="How often a running consumer of this group asks Kafka "
        "for messages, at the longest — one request is cut off after this "
        "many seconds and the next follows once its batch is handled, sooner "
        "when messages are waiting. A fixed property of the platform's "
        "consumer, the same on every deployment. `polling` turns false once "
        f"the last poll is more than {POLLING_INTERVALS} of these old. `null` "
        "for a group reporting a recorded constant and for an unknown group.",
    )


@tool(
    "get_consumer_lag",
    description=(
        "Read the last-emitted Kafka consumer lag for one of the "
        "platform's consumer groups. Known consumer groups: "
        "worker-dispatcher, dependency-resolver, billing-consumer, "
        "orders-consumer, notifications-consumer, analytics-consumer, "
        "payments-consumer, shipping-consumer, healthy-consumer.\n"
        "FRESHNESS: cached in Redis, never a live Kafka query, and the "
        "refresh behaviour differs per group — read `source` before "
        "reasoning about change over time.\n"
        "  - `worker-dispatcher` and `dependency-resolver` (source: live) "
        "are re-measured by a background pass whose interval is "
        "deployment-configured, and cached under a TTL of a few passes. "
        "THE INTERVAL IS NOT A NUMBER YOU CAN ASSUME: `age_seconds` says how "
        "old this reading is, and the gaps between `recent_samples` how often "
        "this deployment measures. A reading can trail a change by up to one "
        "interval in either direction. These are the only two groups whose "
        "lag moves. `worker-dispatcher` consumes submitted jobs and runs "
        "them. `dependency-resolver` consumes job completions and moves a "
        "waiting job whose parent jobs have all completed on to run; on a "
        "quiet platform its lag reads flat whether or not anything is "
        "consuming.\n"
        "  - The other seven (source: static) report a recorded constant "
        "that nothing refreshes. Re-reading one after an action returns the "
        "same number — expected, not evidence the action failed — and their "
        "lag will never be seen to grow.\n"
        "ONE CALL SHOWS THE TREND. `recent_samples` holds the measurements of "
        f"the last 15 minutes, newest first, at most {LAG_SAMPLES_AGENT_CAP} "
        "of them, each with its own time, the current one included: compare "
        "them to decide whether lag is climbing, draining or flat. A second "
        "call is not more evidence: two calls inside one interval return the "
        "SAME number and `measured_at`, which means 'not re-measured yet', "
        "never 'not moving'; a newer number exists once `age_seconds` "
        "exceeds the gap between the newest two samples. On the static "
        "groups `measured_at` and `age_seconds` are null and "
        "`recent_samples` is empty (a constant was never measured at a "
        "moment); on a live group an empty list is missing history, not a "
        "flat line.\n"
        "IS IT CONSUMING? Read `polling`: a dead consumer reads `polling: "
        "false` while `lag`, `lag_known`, `source` and `age_seconds` can all "
        "still look healthy (a lag of 0 included), and "
        "`last_poll_age_seconds` is the number behind that verdict. A stopped "
        "consumer keeps its Kafka assignment for minutes, so its lag goes on "
        "reporting — and climbs while work arrives — rather than going null; "
        "neither a lag nor a null proves a consumer is alive. `polling: null` "
        "(the static groups, or no poll time recorded) is unknown, never "
        "evidence either way.\n"
        "UNKNOWN IS NOT ZERO. `lag_known: false` with `lag: null` means the "
        "platform could not determine lag (nothing recorded, unknown group, "
        "consumer not started, no partition assignment, or the query "
        "errored); it is never reported as 0, because a fabricated 0 would "
        "read as healthy. A measured `lag: 0` with `lag_known: true` is the "
        "opposite finding — the group is caught up. `source: static` with "
        "`lag_known: false` is an environment problem to report, not a fault "
        "to diagnose."
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
        # The newest few, not the whole window: the operator console reads every point
        # in the window (`GET /admin/consumer-lag`), and this surface is paid for in the
        # caller's context on every read, so its SIZE must not change with a
        # deployment's sampling interval (WO-R3-338).
        recent_samples=[
            LagSample(lag=s.lag, measured_at=s.measured_at)
            for s in reading.recent_samples[:LAG_SAMPLES_AGENT_CAP]
        ],
        last_poll_at=reading.last_poll_at,
        last_poll_age_seconds=reading.last_poll_age_seconds,
        polling=reading.polling,
        poll_interval_seconds=reading.poll_interval_seconds,
    )


__all__ = [
    "LAG_SAMPLES_AGENT_CAP",
    "STATIC_LAG_GROUPS",
    "LIVE_REFRESHED_GROUP",
    "LIVE_REFRESHED_GROUPS",
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
