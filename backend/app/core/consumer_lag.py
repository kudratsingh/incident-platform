"""
One reading of a Kafka consumer group's lag, from the cache the metrics loop writes.

Extracted from `app/mcp/tools/consumer_lag.py` (WO-R3-312) because the operator console
needs the same number the agent surface reports, and `app.api` may not import `app.mcp`
— the import-linter contract is one-directional (ADR 0006). So the reading lives here,
in core, and both surfaces build their own response model from it. That is the whole
point: two callers, one arithmetic, no chance of the console and the tool disagreeing
about whether a lag is known.

Since WO-R3-328 the *writer* lives here too (`record_lag_sample`), for the same reason
one step further: the window's key, cap and TTL were literals in the worker mirrored by
literals in the reader, and a 5-sample cap that drifted from a 15-sample reader would
be invisible until a console drew a chart shorter than its axis. The worker may import
core (it already imports six other core modules); core still imports no worker.

Nothing here decides what `unknown` means for a caller. It reports what it found —
including that it found nothing — and the surfaces say so in their own words.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from app.core.logging import get_logger

logger = get_logger(__name__)

# Must match `_metrics_loop` in `app/workers/dispatcher.py` and the seed script.
CONSUMER_LAG_KEY_PREFIX = "kafka:consumer_lag:"

# The metrics loop's timestamped window, beside the value key because
# `check_backpressure` fixes that shape. One window per group, keyed by group, though
# only the continuously-refreshed group has a writer.
LAG_SAMPLES_SUFFIX = ":samples"

# How much history the window holds, and the three numbers that follow from it
# (WO-R3-328). Fifteen minutes because that is the span an operator watching a fault go
# in and drain out needs on one chart: five samples (~5 min) meant the console had to
# stitch a window together client-side, which made the chart restart on every reload.
LAG_SAMPLES_WINDOW_SECONDS = 900
# One sample per metrics pass, and the pass is every ~60s
# (`dispatcher._METRICS_LOOP_INTERVAL`), so the count is the window over the interval.
LAG_SAMPLES_KEEP = 15
# Longer than the window on purpose, unlike the value key's 90s. The value is what
# `check_backpressure` reads and it must be fresh-or-absent; the window is history, and
# history that vanished 90 seconds after the metrics pass stopped would take the chart
# with it exactly when an operator is looking for the gap. Still a TTL, so a window
# nothing refreshes disappears rather than sitting there being read as current.
LAG_SAMPLES_TTL = LAG_SAMPLES_WINDOW_SECONDS + 180

# The one group genuinely refreshed: every ~60s, 90s TTL, so its number moves.
LIVE_REFRESHED_GROUP = "worker-dispatcher"

# Groups whose lag is a recorded constant, from
# `seed_eval_fixtures._seed_consumer_lag`. Nothing refreshes them, so the value
# does not move. Named "static" for what the WIRE calls them: ADR 0012 rule 1 bans
# lab words there, and `test_no_lab_vocabulary_on_non_chaos_tool_surface` guards it.
STATIC_LAG_GROUPS = (
    "billing-consumer",
    "orders-consumer",
    "notifications-consumer",
    "analytics-consumer",
    "payments-consumer",
    "shipping-consumer",
    "healthy-consumer",
)

# Advertised in the tool's input description so `tools/list` gives a menu, and the set
# the console's own listing walks. A name outside it returns `lag: null`.
SEEDED_CONSUMER_GROUPS = (LIVE_REFRESHED_GROUP,) + STATIC_LAG_GROUPS

LagSource = Literal["live", "static", "unrecognized"]


def lag_key(group: str) -> str:
    return f"{CONSUMER_LAG_KEY_PREFIX}{group}"


def samples_key(group: str) -> str:
    return f"{lag_key(group)}{LAG_SAMPLES_SUFFIX}"


def source_for(group: str) -> LagSource:
    if group == LIVE_REFRESHED_GROUP:
        return "live"
    if group in STATIC_LAG_GROUPS:
        return "static"
    return "unrecognized"


@dataclass(frozen=True)
class LagSampleReading:
    """One past measurement of a group's lag."""

    lag: int
    measured_at: datetime


@dataclass(frozen=True)
class LagReading:
    """Everything one group's cache entries say, with the ages already taken.

    `lag_known` comes from the read, not from the group name: `source` is what tells a
    caller whether an absent value is an environment problem or an unknown group.
    """

    consumer_group: str
    lag: int | None
    lag_known: bool
    source: LagSource
    cache_key: str
    measured_at: datetime | None
    age_seconds: int | None
    recent_samples: tuple[LagSampleReading, ...]


def parse_lag(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def parse_samples(raw: Any) -> tuple[LagSampleReading, ...]:
    """Decode the recorded window, dropping anything unreadable.

    Partial decoding beats none — one corrupt entry must not hide four good
    measurements — and the result is re-sorted newest-first, as both surfaces promise.
    """
    if raw is None:
        return ()
    if isinstance(raw, bytes | bytearray):
        raw = raw.decode(errors="replace")
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("consumer lag window unreadable", extra={"kind": "json"})
        return ()
    if not isinstance(loaded, list):
        return ()

    samples: list[LagSampleReading] = []
    for entry in loaded:
        if not isinstance(entry, dict):
            continue
        lag = parse_lag(entry.get("lag"))
        measured_at = parse_measured_at(entry.get("measured_at"))
        if lag is None or measured_at is None:
            continue
        samples.append(LagSampleReading(lag=lag, measured_at=measured_at))

    samples.sort(key=lambda s: s.measured_at, reverse=True)
    return tuple(samples[:LAG_SAMPLES_KEEP])


def parse_measured_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # A naive stamp means a writer dropped the offset; the clock is UTC everywhere.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def record_lag_sample(
    redis: Any, lag: int, *, group: str = LIVE_REFRESHED_GROUP
) -> None:
    """Prepend one timestamped measurement to `group`'s capped window.

    Read-modify-write on purpose: the window is a diagnostic aid, not a correctness
    input, so a lost race costs one sample. Anything stored that is not a JSON list is
    replaced rather than parsed around.

    One sample per call, and the caller calls once per metrics pass — the cap is a count
    of passes, so a caller that recorded twice per pass would halve the window's span
    without changing its length.
    """
    measured_at = datetime.now(UTC).isoformat()
    key = samples_key(group)
    samples: list[Any] = []
    raw = await redis.get(key)
    if raw is not None:
        if isinstance(raw, bytes | bytearray):
            raw = raw.decode()
        try:
            loaded = json.loads(raw)
        except (TypeError, ValueError):
            loaded = None
        if isinstance(loaded, list):
            samples = [s for s in loaded if isinstance(s, dict)]
    samples.insert(0, {"lag": int(lag), "measured_at": measured_at})
    del samples[LAG_SAMPLES_KEEP:]
    await redis.set(key, json.dumps(samples), ex=LAG_SAMPLES_TTL)


async def read_lag(redis: Any, group: str) -> LagReading:
    """The current reading for one group. Never raises on missing data; an absent
    value is reported as unknown rather than as zero."""
    key = lag_key(group)
    lag = parse_lag(await redis.get(key))
    source = source_for(group)

    # Only the continuously-refreshed group has a recorded window; nothing writes one
    # for a constant, so nothing reads one either.
    samples: tuple[LagSampleReading, ...] = ()
    if source == "live":
        samples = parse_samples(await redis.get(samples_key(group)))

    # The newest sample dates the current reading only if it IS that reading. The loop
    # writes the value then the window, so a read between the two sees a number with no
    # recorded time — reported unknown, never guessed.
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

    return LagReading(
        consumer_group=group,
        lag=lag,
        lag_known=lag is not None,
        source=source,
        cache_key=key,
        measured_at=measured_at,
        age_seconds=age_seconds,
        recent_samples=samples,
    )


__all__ = [
    "CONSUMER_LAG_KEY_PREFIX",
    "LAG_SAMPLES_KEEP",
    "LAG_SAMPLES_SUFFIX",
    "LAG_SAMPLES_TTL",
    "LAG_SAMPLES_WINDOW_SECONDS",
    "LIVE_REFRESHED_GROUP",
    "SEEDED_CONSUMER_GROUPS",
    "STATIC_LAG_GROUPS",
    "LagReading",
    "LagSampleReading",
    "LagSource",
    "lag_key",
    "parse_lag",
    "parse_measured_at",
    "parse_samples",
    "read_lag",
    "record_lag_sample",
    "samples_key",
    "source_for",
]
