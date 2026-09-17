"""The consumer-lag reading carries time (WO-R3-254).

`get_consumer_lag` used to return one bare number. The metrics loop
overwrites that number about once a minute, so a caller that cannot
sleep — the agent is exactly that caller — could not tell a lag that is
climbing from one that is flat: three reads inside 25 seconds returned
the same cached value, which reads as "not moving" and really means "not
re-measured yet". A live run was lost to that reading on 2026-09-17.

So the loop records WHEN it measured each value and keeps a short capped
window of recent measurements, and the tool returns `measured_at`,
`age_seconds` and `recent_samples` beside the number.

Two properties these tests exist to hold:

  * **The value key is untouched.** `kafka:consumer_lag:worker-dispatcher`
    keeps its exact shape — an integer under a 90s TTL — because
    `check_backpressure` and every other reader parse it. The window is a
    second key.
  * **Nothing is fabricated.** No recorded measurement, no `measured_at`
    and no samples; the current `lag` is still returned. An empty window
    is missing history, never evidence of a steady lag.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.mcp.registry import ToolContext
from app.mcp.tools.consumer_lag import (
    _LAG_SAMPLES_KEEP,
    LIVE_REFRESHED_GROUP,
    STATIC_LAG_GROUPS,
    GetConsumerLagInput,
    _samples_key,
    get_consumer_lag,
)
from app.utils.backpressure import BACKPRESSURE_LAG_KEY
from app.workers.dispatcher import (
    BACKPRESSURE_LAG_TTL,
    LAG_SAMPLES_KEEP,
    LAG_SAMPLES_KEY,
    _metrics_loop,
    _record_lag_sample,
)


class _RedisStub:
    """get/set only — the whole surface both sides of this feature use."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.store: dict[str, str] = dict(values or {})
        self.ttls: dict[str, int | None] = {}
        self.fail_set_on: set[str] = set()

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        if key in self.fail_set_on:
            raise ConnectionError("redis write refused")
        self.store[key] = str(value)
        self.ttls[key] = ex
        return True


def _window(stub: _RedisStub) -> list[dict[str, Any]]:
    raw = stub.store.get(LAG_SAMPLES_KEY)
    return [] if raw is None else json.loads(raw)


def _sample(lag: int, *, seconds_ago: float) -> dict[str, Any]:
    at = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    return {"lag": lag, "measured_at": at.isoformat()}


def _ctx(redis: Any) -> ToolContext:
    return ToolContext(db=None, redis=redis, principal=None)  # type: ignore[arg-type]


async def _call(redis: Any, group: str = LIVE_REFRESHED_GROUP):  # type: ignore[no-untyped-def]
    return await get_consumer_lag(
        GetConsumerLagInput(consumer_group=group), _ctx(redis)
    )


async def _one_loop_pass(redis: Any, lag: int | None) -> None:
    """Run exactly one iteration of `_metrics_loop` against `redis`."""
    consumer = MagicMock()
    consumer.in_flight = set()
    consumer.consumer_lag = AsyncMock(return_value=lag)

    calls = {"n": 0}

    async def _sleep_once(_):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    with (
        patch("app.workers.dispatcher.asyncio.sleep", _sleep_once),
        patch(
            "app.workers.dispatcher.queue.delayed_length", AsyncMock(return_value=0)
        ),
        patch("app.workers.dispatcher.metrics.emit_gauge", AsyncMock()),
    ):
        await _metrics_loop(redis, consumer)


# ---------------------------------------------------------------------------
# The writer: the metrics loop records when it measured
# ---------------------------------------------------------------------------


async def test_the_loop_records_the_value_and_the_time_it_measured_it() -> None:
    redis = _RedisStub()

    await _one_loop_pass(redis, 29)

    # The value key is unchanged in shape: still the bare integer, still
    # the 90s TTL backpressure's fail-open behaviour depends on.
    assert redis.store[BACKPRESSURE_LAG_KEY] == "29"
    assert redis.ttls[BACKPRESSURE_LAG_KEY] == BACKPRESSURE_LAG_TTL

    window = _window(redis)
    assert len(window) == 1
    assert window[0]["lag"] == 29
    measured_at = datetime.fromisoformat(window[0]["measured_at"])
    assert measured_at.tzinfo is not None, "a stamp with no offset is ambiguous"
    assert abs((datetime.now(UTC) - measured_at).total_seconds()) < 5
    # Same TTL as the value, so the pair is fresh-or-absent together.
    assert redis.ttls[LAG_SAMPLES_KEY] == BACKPRESSURE_LAG_TTL


async def test_the_window_is_capped_and_newest_first() -> None:
    """Five is the whole window; the sixth pass drops the oldest.

    Unbounded history would grow one entry a minute for as long as the
    worker is up, in a value every reader deserializes whole."""
    redis = _RedisStub()

    for lag in range(LAG_SAMPLES_KEEP + 3):
        await _one_loop_pass(redis, lag)

    window = _window(redis)
    assert len(window) == LAG_SAMPLES_KEEP
    assert [s["lag"] for s in window] == list(
        range(LAG_SAMPLES_KEEP + 2, LAG_SAMPLES_KEEP - 3, -1)
    ), "newest first, oldest dropped"


async def test_an_unknown_lag_records_nothing_at_all() -> None:
    """`consumer_lag() is None` means unknown, not 0 — and a fabricated
    sample would be worse than a fabricated value, because it would date
    a measurement that never happened."""
    redis = _RedisStub()

    await _one_loop_pass(redis, None)

    assert BACKPRESSURE_LAG_KEY not in redis.store
    assert LAG_SAMPLES_KEY not in redis.store


async def test_an_unreadable_window_is_replaced_not_parsed_around() -> None:
    redis = _RedisStub({LAG_SAMPLES_KEY: "{not json at all"})

    await _one_loop_pass(redis, 7)

    assert [s["lag"] for s in _window(redis)] == [7]


async def test_a_failed_window_write_leaves_the_value_written() -> None:
    """The history is a diagnostic aid; backpressure's key is not. A
    Redis refusal on the window must not cost the value write or kill
    the loop."""
    redis = _RedisStub()
    redis.fail_set_on.add(LAG_SAMPLES_KEY)

    await _one_loop_pass(redis, 31)  # must not raise

    assert redis.store[BACKPRESSURE_LAG_KEY] == "31"
    assert LAG_SAMPLES_KEY not in redis.store


async def test_record_lag_sample_accepts_a_bytes_window() -> None:
    """`decode_responses` is a client setting, so the stored value comes
    back as bytes on some connections and str on others."""

    class _BytesRedis(_RedisStub):
        async def get(self, key: str) -> Any:
            raw = self.store.get(key)
            return None if raw is None else raw.encode()

    redis = _BytesRedis({LAG_SAMPLES_KEY: json.dumps([_sample(4, seconds_ago=60)])})

    await _record_lag_sample(redis, 9)

    assert [s["lag"] for s in _window(redis)] == [9, 4]


# ---------------------------------------------------------------------------
# The reader: one call shows the trend
# ---------------------------------------------------------------------------


async def test_the_live_group_carries_its_measurement_time_and_the_window() -> None:
    redis = _RedisStub(
        {
            BACKPRESSURE_LAG_KEY: "290",
            LAG_SAMPLES_KEY: json.dumps(
                [
                    _sample(290, seconds_ago=10),
                    _sample(180, seconds_ago=70),
                    _sample(95, seconds_ago=130),
                ]
            ),
        }
    )

    out = await _call(redis)

    assert out.lag == 290
    assert out.lag_known is True
    assert out.source == "live"
    assert out.measured_at is not None
    assert out.age_seconds is not None and 5 <= out.age_seconds <= 20
    # The trend is readable from this one response — which is the whole
    # point: the caller cannot wait for the next measurement.
    assert [s.lag for s in out.recent_samples] == [290, 180, 95]
    assert out.recent_samples[0].measured_at == out.measured_at


async def test_samples_are_returned_newest_first_whatever_the_stored_order() -> None:
    """The description promises newest first, so the reader sorts rather
    than trusting the writer's order (two worker replicas both record)."""
    redis = _RedisStub(
        {
            BACKPRESSURE_LAG_KEY: "12",
            LAG_SAMPLES_KEY: json.dumps(
                [
                    _sample(5, seconds_ago=200),
                    _sample(12, seconds_ago=3),
                    _sample(9, seconds_ago=100),
                ]
            ),
        }
    )

    out = await _call(redis)

    assert [s.lag for s in out.recent_samples] == [12, 9, 5]
    assert out.measured_at == out.recent_samples[0].measured_at


async def test_a_value_with_no_recorded_window_is_returned_honestly() -> None:
    """Value present, window absent — the state right after a reset, and
    for the first minute of a fresh worker. The number is real; the time
    it was measured is not known, and is not guessed."""
    redis = _RedisStub({BACKPRESSURE_LAG_KEY: "29"})

    out = await _call(redis)

    assert out.lag == 29
    assert out.lag_known is True
    assert out.measured_at is None
    assert out.age_seconds is None
    assert out.recent_samples == []


async def test_a_window_that_disagrees_with_the_value_dates_nothing() -> None:
    """The newest sample dates the current number only if it IS the
    current number. The loop writes value-then-window, so a read landing
    between the two sees a value whose time has not been recorded yet;
    borrowing the older sample's stamp would date a measurement that
    never produced this number."""
    redis = _RedisStub(
        {
            BACKPRESSURE_LAG_KEY: "500",
            LAG_SAMPLES_KEY: json.dumps([_sample(290, seconds_ago=10)]),
        }
    )

    out = await _call(redis)

    assert out.lag == 500
    assert out.measured_at is None
    assert out.age_seconds is None
    # The recorded measurements are still real measurements, so they are
    # still returned.
    assert [s.lag for s in out.recent_samples] == [290]


async def test_an_absent_value_still_reports_what_was_measured_before() -> None:
    """`lag_known: false` keeps its meaning — the platform cannot
    determine the current lag — without throwing away the history that
    says what it was."""
    redis = _RedisStub({LAG_SAMPLES_KEY: json.dumps([_sample(44, seconds_ago=30)])})

    out = await _call(redis)

    assert out.lag is None
    assert out.lag_known is False
    assert out.measured_at is None
    assert [s.lag for s in out.recent_samples] == [44]


@pytest.mark.parametrize("group", STATIC_LAG_GROUPS)
async def test_a_group_reporting_a_constant_has_no_time_and_no_window(
    group: str,
) -> None:
    """A recorded constant was never measured at a moment. Reporting a
    time for it would invent one, and reporting a window would invite
    exactly the trend reading the number cannot support."""
    redis = _RedisStub(
        {
            f"kafka:consumer_lag:{group}": "15000",
            # Present on purpose: even if something wrote a window for a
            # constant, the tool must not read it.
            _samples_key(group): json.dumps([_sample(15000, seconds_ago=5)]),
        }
    )

    out = await _call(redis, group)

    assert out.lag == 15000
    assert out.source == "static"
    assert out.measured_at is None
    assert out.age_seconds is None
    assert out.recent_samples == []


async def test_an_unrecognized_group_is_unchanged() -> None:
    out = await _call(_RedisStub(), "nope-consumer")

    assert out.lag is None
    assert out.source == "unrecognized"
    assert out.measured_at is None
    assert out.recent_samples == []


async def test_malformed_entries_are_dropped_and_good_ones_kept() -> None:
    """One corrupt entry must not hide four good measurements, and an
    entry with no usable time is not a sample — it would have to be
    dated to be one."""
    redis = _RedisStub(
        {
            BACKPRESSURE_LAG_KEY: "7",
            LAG_SAMPLES_KEY: json.dumps(
                [
                    _sample(7, seconds_ago=5),
                    "not-an-object",
                    {"lag": 6},  # no time
                    {"measured_at": datetime.now(UTC).isoformat()},  # no lag
                    {"lag": 5, "measured_at": "yesterday"},
                    _sample(4, seconds_ago=120),
                ]
            ),
        }
    )

    out = await _call(redis)

    assert [s.lag for s in out.recent_samples] == [7, 4]
    assert out.measured_at is not None


async def test_an_unreadable_window_reads_as_no_history() -> None:
    redis = _RedisStub({BACKPRESSURE_LAG_KEY: "7", LAG_SAMPLES_KEY: "]["})

    out = await _call(redis)

    assert out.lag == 7
    assert out.recent_samples == []
    assert out.measured_at is None


async def test_the_reader_caps_the_window_it_returns() -> None:
    """Defence against a key written by something other than this loop:
    the advertised shape is "the last few", so the tool bounds what it
    hands back rather than trusting the stored length."""
    redis = _RedisStub(
        {
            BACKPRESSURE_LAG_KEY: "99",
            LAG_SAMPLES_KEY: json.dumps(
                [_sample(99 - i, seconds_ago=i * 60) for i in range(20)]
            ),
        }
    )

    out = await _call(redis)

    assert len(out.recent_samples) == _LAG_SAMPLES_KEEP


async def test_age_seconds_is_never_negative() -> None:
    """Clock skew between the writer and the reader must not produce a
    negative age, which would read as a measurement from the future."""
    redis = _RedisStub(
        {
            BACKPRESSURE_LAG_KEY: "3",
            LAG_SAMPLES_KEY: json.dumps([_sample(3, seconds_ago=-30)]),
        }
    )

    out = await _call(redis)

    assert out.age_seconds == 0


# ---------------------------------------------------------------------------
# The three literals that must not drift
# ---------------------------------------------------------------------------


def test_the_reader_and_the_writer_name_the_same_window_key() -> None:
    """The MCP process must not import the worker package (ADR 0006), so
    the key is a literal on both sides. This is the only thing standing
    between that duplication and a silent divergence — the same reason
    `test_clear_scheduled_replays_drops_pending_timers` asserts against
    the scheduler's constants."""
    assert _samples_key(LIVE_REFRESHED_GROUP) == LAG_SAMPLES_KEY
    assert LAG_SAMPLES_KEY == f"{BACKPRESSURE_LAG_KEY}:samples"
    assert _LAG_SAMPLES_KEEP == LAG_SAMPLES_KEEP
