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

Since WO-R3-338 the *clock* lives here as well. The metrics pass was a hard-coded 60 s
and the demo stack runs it at 5 s (owner decision O-35 — nobody watching a demo waits a
minute for a number to move), so the interval is a setting and the two numbers that
follow from it are derived from it rather than restated: the value key's TTL, and how
many samples fifteen minutes of history holds. One module owns all three, because a TTL
that no longer exceeded the interval would blank `check_backpressure`'s reading on a
healthy stack and nothing would say why.

Nothing here decides what `unknown` means for a caller. It reports what it found —
including that it found nothing — and the surfaces say so in their own words.
"""

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from app.config import get_settings
from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.config import Settings

logger = get_logger(__name__)

# Must match `_metrics_loop` in `app/workers/dispatcher.py` and the seed script.
CONSUMER_LAG_KEY_PREFIX = "kafka:consumer_lag:"

# The metrics loop's timestamped window, beside the value key because
# `check_backpressure` fixes that shape. One window per group, keyed by group, though
# only the continuously-refreshed group has a writer.
LAG_SAMPLES_SUFFIX = ":samples"

# How much history the window holds (WO-R3-328). Fifteen minutes because that is the span
# an operator watching a fault go in and drain out needs on one chart: five samples
# (~5 min) meant the console had to stitch a window together client-side, which made the
# chart restart on every reload. A SPAN, not a count: since WO-R3-338 the pass interval is
# a setting, so the same fifteen minutes holds 15 samples at the 60 s default and 180 at
# the demo stack's 5 s. The window is the promise; the density follows the clock.
LAG_SAMPLES_WINDOW_SECONDS = 900
# The absolute bound on the stored ring, so no interval — or a key written by something
# other than the metrics loop — can leave an unbounded list to be read back. It is a
# guard, not the window: at any interval down to 3.75 s the time prune bites first.
LAG_SAMPLES_MAX_ENTRIES = 240

# What the AGENT's surface returns, however many the ring holds: the newest fifteen, which
# is the count and the order `get_consumer_lag` returned before the clock became a setting.
# The two surfaces are deliberately asymmetric. An operator's chart wants every point in
# the window and is drawn once; the agent pays for each sample in context on every read,
# and a reading whose SIZE changed with a deployment's tick would make one stack's
# investigation quietly more expensive than another's for no new information — fifteen
# newest samples answer "climbing, draining or flat" at any interval. `age_seconds` and the
# gaps between the samples still say how often this deployment measures.
LAG_SAMPLES_AGENT_CAP = 15
# Longer than the window on purpose, unlike the value key's. The value is what
# `check_backpressure` reads and it must be fresh-or-absent; the window is history, and
# history that vanished three passes after the metrics loop stopped would take the chart
# with it exactly when an operator is looking for the gap. Still a TTL, so a window
# nothing refreshes disappears rather than sitting there being read as current.
LAG_SAMPLES_TTL = LAG_SAMPLES_WINDOW_SECONDS + 180

# The floor under the configured pass interval. A 0 or a negative would turn the metrics
# loop into a busy-wait against Kafka and Redis, so it is clamped — and clamped HERE, in
# the one function both the loop and `control_loop_pause.tick_interval_seconds` read, so
# the number an operator is told the loop sleeps is the number it sleeps.
MIN_METRICS_INTERVAL_SECONDS = 1.0
# The value key lives three passes. One lost pass must not blank the reading on a healthy
# stack (a null reads as "unknown" and opens the backpressure gate), and three is short
# enough that the key is still fresh-or-absent rather than a minute-old number at a 5 s
# tick. It replaces a fixed 90 s, which was 1.5 passes at 60 s and eighteen at 5 s.
LAG_VALUE_TTL_INTERVALS = 3
# …with a floor for the same reason the interval has one: a sub-second interval must not
# produce a TTL that expires between passes.
MIN_LAG_VALUE_TTL_SECONDS = 15

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


def metrics_interval_seconds(settings: "Settings | None" = None) -> float:
    """Seconds between metrics passes, as configured now — the platform's lag clock.

    Read per pass rather than captured at import, so the demo stack's 5 s needs an env
    var and not a code change (O-35), and so a test can move the clock without patching
    a module constant.
    """
    configured = (settings or get_settings()).metrics_loop_interval_seconds
    return max(MIN_METRICS_INTERVAL_SECONDS, float(configured))


def lag_value_ttl_seconds(settings: "Settings | None" = None) -> int:
    """How long the lag VALUE key lives: three passes, floored.

    Derived rather than fixed because the fixed 90 s said two different things at two
    intervals — 1.5 passes at 60 s, and eighteen passes of stale number at 5 s. The value
    gates `POST /jobs`, so it must be a fresh measurement or absent; `LAG_SAMPLES_TTL`
    keeps the opposite contract for the window beside it.
    """
    return max(
        MIN_LAG_VALUE_TTL_SECONDS,
        math.ceil(LAG_VALUE_TTL_INTERVALS * metrics_interval_seconds(settings)),
    )


def lag_samples_at_interval(interval_seconds: float) -> int:
    """How many samples fifteen minutes of history holds at this pass interval.

    Not a cap the writer enforces — the writer prunes by time — but the number the window
    converges on, so a caller sizing a chart or an assertion has one place to ask.
    """
    interval = max(MIN_METRICS_INTERVAL_SECONDS, float(interval_seconds))
    return min(
        LAG_SAMPLES_MAX_ENTRIES,
        max(1, int(LAG_SAMPLES_WINDOW_SECONDS // interval)),
    )


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
    # Bounded by the absolute cap, not by a count derived from this process's own setting:
    # the reader runs in the API and MCP processes, and a reader that trimmed the window
    # to ITS interval would hide samples the writer kept whenever the two disagreed.
    return tuple(samples[:LAG_SAMPLES_MAX_ENTRIES])


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
    """Prepend one timestamped measurement to `group`'s rolling window.

    Read-modify-write on purpose: the window is a diagnostic aid, not a correctness
    input, so a lost race costs one sample. Anything stored that is not a JSON list is
    replaced rather than parsed around.

    **Pruned by time, not by a count of passes (WO-R3-338).** The old cap was
    `window / 60 s`, which silently became 75 seconds of history the moment the pass
    interval became a setting and the demo stack set it to 5 s. Now a sample leaves when
    it leaves the fifteen minutes, so the span is the promise and the density follows the
    clock; `LAG_SAMPLES_MAX_ENTRIES` remains as an absolute guard. A stored sample whose
    time cannot be read is dropped here rather than carried — with a count it could be
    carried harmlessly (the reader dropped it), with a time prune it would never expire.
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=LAG_SAMPLES_WINDOW_SECONDS)
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
            samples = [
                s
                for s in loaded
                if isinstance(s, dict)
                and (at := parse_measured_at(s.get("measured_at"))) is not None
                and at > cutoff
            ]
    samples.insert(0, {"lag": int(lag), "measured_at": now.isoformat()})
    del samples[LAG_SAMPLES_MAX_ENTRIES:]
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
    "LAG_SAMPLES_AGENT_CAP",
    "LAG_SAMPLES_MAX_ENTRIES",
    "LAG_SAMPLES_SUFFIX",
    "LAG_SAMPLES_TTL",
    "LAG_SAMPLES_WINDOW_SECONDS",
    "LAG_VALUE_TTL_INTERVALS",
    "LIVE_REFRESHED_GROUP",
    "MIN_LAG_VALUE_TTL_SECONDS",
    "MIN_METRICS_INTERVAL_SECONDS",
    "SEEDED_CONSUMER_GROUPS",
    "STATIC_LAG_GROUPS",
    "LagReading",
    "LagSampleReading",
    "LagSource",
    "lag_key",
    "lag_samples_at_interval",
    "lag_value_ttl_seconds",
    "metrics_interval_seconds",
    "parse_lag",
    "parse_measured_at",
    "parse_samples",
    "read_lag",
    "record_lag_sample",
    "samples_key",
    "source_for",
]
