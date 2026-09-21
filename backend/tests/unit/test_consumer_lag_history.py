"""The consumer-lag reading carries time (WO-R3-254)."""

from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import Settings
from app.core.consumer_lag import (
    LAG_SAMPLES_AGENT_CAP,
    LAG_SAMPLES_MAX_ENTRIES,
    LAG_SAMPLES_WINDOW_SECONDS,
    lag_samples_at_interval,
    lag_value_ttl_seconds,
    metrics_interval_seconds,
    read_lag,
    record_lag_sample,
)
from app.mcp.registry import ToolContext
from app.mcp.tools.consumer_lag import (
    LIVE_REFRESHED_GROUP,
    STATIC_LAG_GROUPS,
    GetConsumerLagInput,
    _samples_key,
    get_consumer_lag,
)
from app.utils.backpressure import BACKPRESSURE_LAG_KEY
from app.workers.dispatcher import (
    LAG_SAMPLES_KEY,
    LAG_SAMPLES_TTL,
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


async def _one_loop_pass(
    redis: Any, lag: int | None, *, settings: Settings | None = None
) -> list[float]:
    """Run exactly one iteration of `_metrics_loop` against `redis`.

    Returns the sleeps it asked for, because the pass interval is a setting now and the
    sleep is the only place a wrong one is observable.
    """
    consumer = MagicMock()
    consumer.in_flight = set()
    consumer.consumer_lag = AsyncMock(return_value=lag)

    calls = {"n": 0}
    slept: list[float] = []

    async def _sleep_once(seconds):  # type: ignore[no-untyped-def]
        slept.append(seconds)
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    with ExitStack() as stack:
        stack.enter_context(patch("app.workers.dispatcher.asyncio.sleep", _sleep_once))
        stack.enter_context(
            patch(
                "app.workers.dispatcher.queue.delayed_length",
                AsyncMock(return_value=0),
            )
        )
        stack.enter_context(
            patch("app.workers.dispatcher.metrics.emit_gauge", AsyncMock())
        )
        # The alert rules ride the same tick (ADR 0039); `test_alert_rules.py` owns them.
        stack.enter_context(
            patch(
                "app.workers.dispatcher.alert_rules.evaluate_alert_rules",
                AsyncMock(return_value=None),
            )
        )
        if settings is not None:
            stack.enter_context(
                patch("app.core.consumer_lag.get_settings", lambda: settings)
            )
        await _metrics_loop(redis, consumer, MagicMock())
    return slept


# The writer: the metrics loop records when it measured


async def test_the_loop_records_the_value_and_the_time_it_measured_it() -> None:
    redis = _RedisStub()

    await _one_loop_pass(redis, 29)

    # The value key is unchanged in shape: still the bare integer, still a TTL short
    # enough that backpressure reads a fresh number or none at all.
    assert redis.store[BACKPRESSURE_LAG_KEY] == "29"
    assert redis.ttls[BACKPRESSURE_LAG_KEY] == lag_value_ttl_seconds()

    window = _window(redis)
    assert len(window) == 1
    assert window[0]["lag"] == 29
    measured_at = datetime.fromisoformat(window[0]["measured_at"])
    assert measured_at.tzinfo is not None, "a stamp with no offset is ambiguous"
    assert abs((datetime.now(UTC) - measured_at).total_seconds()) < 5
    # NOT the value's TTL since WO-R3-328: the value must be fresh-or-absent because
    # backpressure reads it, and the window is history an operator needs to still be
    # there when the pass that would have refreshed it is the thing that stopped.
    assert redis.ttls[LAG_SAMPLES_KEY] == LAG_SAMPLES_TTL
    assert LAG_SAMPLES_TTL > lag_value_ttl_seconds()


async def test_the_window_drops_what_left_the_fifteen_minutes_and_keeps_the_rest() -> (
    None
):
    """The window is time-based, not a count of passes (WO-R3-338): at a 5 s tick the same
    fifteen minutes hold 180 samples, and the count is what the tick decides."""
    redis = _RedisStub(
        {
            LAG_SAMPLES_KEY: json.dumps(
                [
                    _sample(7, seconds_ago=LAG_SAMPLES_WINDOW_SECONDS + 30),
                    _sample(8, seconds_ago=LAG_SAMPLES_WINDOW_SECONDS + 90),
                ]
                + [_sample(1, seconds_ago=5)]
            )
        }
    )

    await _one_loop_pass(redis, 2)

    window = _window(redis)
    assert [s["lag"] for s in window] == [2, 1], (
        "a sample older than the window is history the window no longer covers"
    )


async def test_the_window_is_newest_first_and_bounded_however_fast_the_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every sample is inside the window, so only the absolute cap can bound the ring —
    it exists so a pathological interval cannot write an unbounded list."""
    redis = _RedisStub()
    passes = LAG_SAMPLES_MAX_ENTRIES + 3

    for lag in range(passes):
        await record_lag_sample(redis, lag)

    window = _window(redis)
    assert len(window) == LAG_SAMPLES_MAX_ENTRIES
    assert [s["lag"] for s in window] == list(
        range(passes - 1, passes - 1 - LAG_SAMPLES_MAX_ENTRIES, -1)
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


# The reader: one call shows the trend


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
    """Defence against a key written by something other than this loop: the shared reader
    bounds what it hands back at the absolute cap rather than trusting the stored length.

    The cap that belongs to a *surface* is a separate decision, one level up —
    `test_the_agents_window_is_capped_where_the_operators_is_not` owns that.
    """
    redis = _RedisStub(
        {
            BACKPRESSURE_LAG_KEY: "99",
            LAG_SAMPLES_KEY: json.dumps(
                [
                    _sample(99 - i, seconds_ago=i)
                    for i in range(LAG_SAMPLES_MAX_ENTRIES + 20)
                ]
            ),
        }
    )

    reading = await read_lag(redis, LIVE_REFRESHED_GROUP)

    assert len(reading.recent_samples) == LAG_SAMPLES_MAX_ENTRIES


async def test_the_agents_window_is_capped_where_the_operators_is_not() -> None:
    """The two surfaces read the same ring and return different amounts of it, on purpose.

    An operator's chart wants every point in the fifteen minutes and is drawn once. The
    agent pays for each sample in its context on every read, and a reading whose SIZE
    changed with a deployment's tick would make one stack's investigation quietly more
    expensive than another's for no new information — so the agent's surface returns the
    newest fifteen, the count it returned before the clock became a setting, and says so.
    """
    redis = _RedisStub()
    # A 5 s tick for fifteen minutes: the ring the demo stack really holds.
    for lag in range(180):
        await record_lag_sample(redis, lag)
    assert len(_window(redis)) == 180, "the writer keeps the whole time window"

    # What the operator's console reads (`GET /admin/consumer-lag` builds from this).
    operator = await read_lag(redis, LIVE_REFRESHED_GROUP)
    assert len(operator.recent_samples) == 180

    # What the agent reads.
    agent = await _call(redis)
    assert len(agent.recent_samples) == LAG_SAMPLES_AGENT_CAP == 15
    assert [s.lag for s in agent.recent_samples] == list(range(179, 164, -1)), (
        "the newest fifteen, newest first — not the oldest fifteen"
    )
    assert [s.lag for s in agent.recent_samples] == [
        s.lag for s in operator.recent_samples[:LAG_SAMPLES_AGENT_CAP]
    ], "one ring, one order, two lengths"

    # And the description says which, because a cap the caller cannot see is worse than
    # an error (CLAUDE.md: never promise completeness you cap).
    surface = _tool_description() + json.dumps(
        (await _call(redis)).model_json_schema()
    )
    assert f"at most {LAG_SAMPLES_AGENT_CAP} of them" in surface


def _tool_description() -> str:
    from app.mcp.registry import list_tools

    return next(t.description for t in list_tools() if t.name == "get_consumer_lag")


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


# The three literals that must not drift


def test_the_window_spans_the_fifteen_minutes_it_advertises() -> None:
    """The window is the promise; the sample count follows from the tick (WO-R3-338). At
    the 60 s default that is the fifteen samples this window has always held; at the demo
    stack's 5 s it is 180 of them, on the same axis."""
    assert LAG_SAMPLES_WINDOW_SECONDS == 15 * 60
    assert lag_samples_at_interval(60.0) == 15
    assert lag_samples_at_interval(5.0) == 180
    assert lag_samples_at_interval(5.0) <= LAG_SAMPLES_MAX_ENTRIES, (
        "the absolute cap must not truncate the demo stack's window"
    )
    # A pathological interval is bounded rather than trusted.
    assert lag_samples_at_interval(0.01) == LAG_SAMPLES_MAX_ENTRIES


def test_the_pass_interval_is_a_setting_the_loop_reads_every_pass() -> None:
    """O-35: nobody watching a demo waits a minute for a number to move. The demo stack
    sets 5 s; the default is unchanged."""
    assert metrics_interval_seconds(Settings(environment="test")) == 60.0
    assert (
        metrics_interval_seconds(
            Settings(environment="test", metrics_loop_interval_seconds=5)
        )
        == 5.0
    )
    # Clamped, so a 0 cannot turn the loop into a busy-wait, and clamped in ONE place so
    # `control_loop_pause.tick_interval_seconds` reports what the loop will really sleep.
    assert (
        metrics_interval_seconds(
            Settings(environment="test", metrics_loop_interval_seconds=0)
        )
        == 1.0
    )


async def test_the_loop_sleeps_the_configured_interval() -> None:
    redis = _RedisStub()

    slept = await _one_loop_pass(
        redis,
        7,
        settings=Settings(environment="test", metrics_loop_interval_seconds=5),
    )

    assert slept[0] == 5.0


async def test_the_value_key_ttl_follows_the_interval_rather_than_a_literal() -> None:
    """The value must be fresh-or-absent because `check_backpressure` gates on it, so its
    TTL is three passes — one lost pass is survivable, a stale number for a minute at a 5 s
    tick is not."""
    fast = Settings(environment="test", metrics_loop_interval_seconds=5)
    assert lag_value_ttl_seconds(fast) == 15
    assert lag_value_ttl_seconds(Settings(environment="test")) == 180

    redis = _RedisStub()
    await _one_loop_pass(redis, 7, settings=fast)
    assert redis.ttls[BACKPRESSURE_LAG_KEY] == 15


async def test_the_writer_keys_the_window_by_group() -> None:
    """One ring buffer per group. Only the continuously-refreshed group has a writer
    today, but the key is the group's — a second writer needs no new shape."""
    redis = _RedisStub()

    await record_lag_sample(redis, 11, group="billing-consumer")

    assert LAG_SAMPLES_KEY not in redis.store
    assert json.loads(redis.store[_samples_key("billing-consumer")])[0]["lag"] == 11


def test_the_reader_and_the_writer_name_the_same_window_key() -> None:
    """The MCP process must not import the worker package (ADR 0006), so
    the key is a literal on both sides. This is the only thing standing
    between that duplication and a silent divergence — the same reason
    `test_clear_scheduled_replays_drops_pending_timers` asserts against
    the scheduler's constants."""
    assert _samples_key(LIVE_REFRESHED_GROUP) == LAG_SAMPLES_KEY
    assert LAG_SAMPLES_KEY == f"{BACKPRESSURE_LAG_KEY}:samples"
