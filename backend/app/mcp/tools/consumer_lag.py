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
  - **A reading carries its time, and the last few readings with it**
    (WO-R3-254). The metrics loop writes one undated integer every ~60s;
    an agent that cannot sleep cannot watch it move, and three reads
    inside 25 seconds returning the same cached number read as "the lag
    is not moving" when they really meant "not re-measured yet". That
    misreading cost a live run. So `measured_at` / `age_seconds` say
    when the returned number was measured, and `recent_samples` returns
    the metrics loop's short recorded window — the trend is available
    from ONE call, which is the only way this caller can get it. None of
    it is ever invented: no measurement time, no `measured_at`; no
    recorded window, no samples.

Requires `telemetry:read`.
"""

import json
from datetime import UTC, datetime
from typing import Any, Literal

from app.core.logging import get_logger
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)

# Prefix must match what the metrics loop (`_metrics_loop` in
# `app/workers/dispatcher.py`) and the eval seed script both use.
# Kept as a module constant so any three call sites stay aligned.
_CONSUMER_LAG_KEY_PREFIX = "kafka:consumer_lag:"


def _redis_key(group: str) -> str:
    return f"{_CONSUMER_LAG_KEY_PREFIX}{group}"


# Where the metrics loop records its short window of timestamped
# measurements, beside the value key rather than inside it — the value
# key's shape is fixed by `check_backpressure`. Mirror of
# `app/workers/dispatcher.py:LAG_SAMPLES_KEY` and `LAG_SAMPLES_KEEP`,
# duplicated here for the same reason the prefix above is: the MCP
# process must not import the worker package. Kept honest by
# `backend/tests/unit/test_consumer_lag_history.py`.
_LAG_SAMPLES_SUFFIX = ":samples"
_LAG_SAMPLES_KEEP = 5


def _samples_key(group: str) -> str:
    return f"{_redis_key(group)}{_LAG_SAMPLES_SUFFIX}"


def _parse_lag(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


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
        "`null` exactly when `measured_at` is null. A new measurement is "
        "taken about every 60s, so below ~60 this reading is the newest "
        "one that exists and re-reading returns the same number.",
    )
    recent_samples: list[LagSample] = Field(
        default_factory=list,
        description="The last few measurements for this group, newest "
        "first, the current one included. Comparing them is how to tell "
        "a climbing lag from a flat one without waiting. Empty for a "
        "group reporting a recorded constant (nothing measures it), and "
        "empty for the continuously-refreshed group when no window has "
        "been recorded yet — an empty list is absence of history, never "
        "evidence the lag is steady.",
    )


def _parse_samples(raw: Any) -> list[LagSample]:
    """Decode the recorded window, dropping anything unreadable.

    A window that will not parse is reported as no window at all: an
    empty list already means "no history recorded", and the caller is
    told that an empty list is never evidence of a steady lag. Partial
    decoding beats none — one corrupt entry must not hide four good
    measurements — and the result is re-sorted newest-first rather than
    trusting the stored order, because that order is what the
    description promises.
    """
    if raw is None:
        return []
    if isinstance(raw, bytes | bytearray):
        raw = raw.decode(errors="replace")
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("consumer lag window unreadable", extra={"kind": "json"})
        return []
    if not isinstance(loaded, list):
        return []

    samples: list[LagSample] = []
    for entry in loaded:
        if not isinstance(entry, dict):
            continue
        lag = _parse_lag(entry.get("lag"))
        measured_at = _parse_measured_at(entry.get("measured_at"))
        if lag is None or measured_at is None:
            continue
        samples.append(LagSample(lag=lag, measured_at=measured_at))

    samples.sort(key=lambda s: s.measured_at, reverse=True)
    return samples[:_LAG_SAMPLES_KEEP]


def _parse_measured_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # A naive stamp can only come from a writer that dropped the offset;
    # the platform's clock is UTC everywhere, so read it as UTC rather
    # than discarding an otherwise good measurement.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


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
        "ONE CALL SHOWS THE TREND. Every response carries `measured_at` "
        "(when this number was measured), `age_seconds` (how old it is), "
        "and `recent_samples` — the last few measurements, newest first, "
        "each with its own time, the current one included. Compare those "
        "samples to decide whether lag is climbing, draining or flat. "
        "That comparison is the evidence; a second call is not, because "
        "a new measurement is taken only about every 60s. Two calls a "
        "few seconds apart therefore return the SAME number with the "
        "SAME `measured_at`, and that repetition means 'not re-measured "
        "yet', never 'not moving' — reading sooner cannot show change "
        "that has not been measured. If you need a genuinely newer "
        "number than the one in front of you, it exists once "
        "`age_seconds` passes ~60; until then this response already "
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
    key = _redis_key(inp.consumer_group)
    lag = _parse_lag(await ctx.redis.get(key))
    source = _source_for(inp.consumer_group)

    # Only the continuously-refreshed group has a recorded window;
    # nothing writes one for a constant, so nothing reads one either.
    samples: list[LagSample] = []
    if source == "live":
        samples = _parse_samples(await ctx.redis.get(_samples_key(inp.consumer_group)))

    # The newest sample dates the current reading only if it IS the
    # current reading. The loop writes the value then the window, so a
    # read landing between the two sees a number whose time has not been
    # recorded yet — and any other writer of the value key is in the same
    # position. Reporting the older sample's time as this number's would
    # be a fabricated measurement, which is the one thing this tool must
    # never do; an unknown time is reported as unknown.
    measured_at = (
        samples[0].measured_at
        if samples and lag is not None and samples[0].lag == lag
        else None
    )
    age_seconds = (
        max(0, int((datetime.now(UTC) - measured_at).total_seconds()))
        if measured_at is not None
        else None
    )

    return GetConsumerLagOutput(
        consumer_group=inp.consumer_group,
        lag=lag,
        # Derived from the read, not from the group name: a tracked
        # group with a missing fixture is just as unknown as an
        # untracked one, and `source` is what tells those apart.
        lag_known=lag is not None,
        source=source,
        cache_key=key,
        measured_at=measured_at,
        age_seconds=age_seconds,
        recent_samples=samples,
    )


__all__ = [
    "STATIC_LAG_GROUPS",
    "LIVE_REFRESHED_GROUP",
    "SEEDED_CONSUMER_GROUPS",
    "GetConsumerLagInput",
    "GetConsumerLagOutput",
    "LagSample",
    "get_consumer_lag",
]
