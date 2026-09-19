"""
One reading of a Kafka consumer group's lag, from the cache the metrics loop writes.

Extracted from `app/mcp/tools/consumer_lag.py` (WO-R3-312) because the operator console
needs the same number the agent surface reports, and `app.api` may not import `app.mcp`
— the import-linter contract is one-directional (ADR 0006). So the reading lives here,
in core, and both surfaces build their own response model from it. That is the whole
point: two callers, one arithmetic, no chance of the console and the tool disagreeing
about whether a lag is known.

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
# `check_backpressure` fixes that shape. Mirrors `dispatcher.py:LAG_SAMPLES_KEY` /
# `LAG_SAMPLES_KEEP` — no worker imports here.
LAG_SAMPLES_SUFFIX = ":samples"
LAG_SAMPLES_KEEP = 5

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
    "samples_key",
    "source_for",
]
