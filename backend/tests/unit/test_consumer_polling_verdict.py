"""The lag reading says whether the consumer is alive: `polling` (WO-R3-373, ADR 0041 amendment).

INC-008 addendum. v0.6.24 made a killed `dependency-resolver` readable — `last_poll_age_seconds`
froze at the kill and grew — and the next paid run read `last_poll_age_seconds 39` beside
`age_seconds 3` as "polling, somewhat elevated" and anchored on the paused sweep's crisp
`paused: true`. A number the reader must threshold against a cadence nobody told it loses to a
boolean. So the platform draws the line itself:

* `poll_interval_seconds` — the cadence the consumer loop really runs at (the base loop's own
  fetch bound; both measured groups run that loop unmodified), null for the recorded constants.
* `polling` — true when, at the platform's last look, the consumer had polled within
  `POLLING_INTERVALS` of those intervals; false beyond them; null when no poll time is on record.

The verdict is taken where the poll time is observed — the metrics pass records the moment it
looked beside the poll time, from the same clock — and decided in ONE function in core that both
surfaces read. Not against the reader's clock: the platform's default pass runs every 60 s, so a
read-time age would flap a healthy consumer to false between passes, and a stopped pass would
make a live consumer look dead (the reason ADR 0041 made the record fresh-or-absent).

Every test reaches new names through their module, so against the code before this order each
one fails on its own assertion or attribute rather than the file failing to import.
"""

from __future__ import annotations

import asyncio
import json
import random
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import Settings
from app.core import consumer_lag as core
from app.core.consumer_lag import (
    DEPENDENCY_RESOLVER_GROUP,
    LIVE_REFRESHED_GROUP,
    LIVE_REFRESHED_GROUPS,
    STATIC_LAG_GROUPS,
    last_poll_key,
    read_lag,
)
from app.mcp.registry import ToolContext, list_tools
from app.mcp.tools.consumer_lag import (
    GetConsumerLagInput,
    GetConsumerLagOutput,
    get_consumer_lag,
)
from app.workers import dispatcher
from app.workers import kafka_consumer as kafka_consumer_module
from app.workers.dependency_resolver import DependencyResolver
from app.workers.dispatcher import JobDispatcherConsumer
from app.workers.kafka_consumer import BaseKafkaConsumer

# Captured before any test patches `asyncio.sleep` globally (the supervisor tests must), so a
# fake Kafka fetch can still hand control back to the event loop.
_REAL_SLEEP = asyncio.sleep

# The exact sentence the description must carry (WO-R3-373 item 2). Pinned whole: a paraphrase
# that drops one of the four "healthy-looking" fields is the misreading INC-008 was.
POLLING_SENTENCE = (
    "IS IT CONSUMING? Read `polling`: a dead consumer reads `polling: false` "
    "while `lag`, `lag_known`, `source` and `age_seconds` can all still look "
    "healthy (a lag of 0 included), and `last_poll_age_seconds` is the number "
    "behind that verdict."
)

# `get_consumer_lag`'s description on v0.6.24, in characters. The order says trim, not grow.
V0_6_24_DESCRIPTION_CHARS = 4_852


class _RedisStub:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.store: dict[str, str] = dict(values or {})
        self.ttls: dict[str, int | None] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self.store[key] = str(value)
        self.ttls[key] = ex
        return True


def _threshold() -> float:
    return float(core.POLLING_INTERVALS * core.POLL_INTERVAL_SECONDS)


async def _observe(
    redis: _RedisStub,
    group: str,
    *,
    polled_at: datetime,
    observed_at: datetime,
) -> core.LagReading:
    """One look by the platform at `observed_at`, then the reading every surface builds from."""
    await core.record_last_poll(redis, polled_at, group=group, observed_at=observed_at)
    return await read_lag(redis, group)


def _consumer(*, lag: int | None, polled_at: datetime | None) -> MagicMock:
    c = MagicMock()
    c.in_flight = set()
    c.consumer_lag = AsyncMock(return_value=lag)
    c.last_poll_at = polled_at
    return c


async def _one_metrics_pass(
    redis: _RedisStub, dispatcher_consumer: Any, resolver: Any | None
) -> None:
    """Exactly one iteration of the shipped `_metrics_loop`."""
    calls = {"n": 0}

    async def _sleep_once(_seconds: float) -> None:
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
        stack.enter_context(
            patch(
                "app.workers.dispatcher.alert_rules.evaluate_alert_rules",
                AsyncMock(return_value=None),
            )
        )
        stack.enter_context(patch("app.workers.dispatcher.record_loop_run", AsyncMock()))
        await dispatcher._metrics_loop(redis, dispatcher_consumer, MagicMock(), resolver)


async def _tool(redis: _RedisStub, group: str) -> GetConsumerLagOutput:
    ctx = ToolContext(db=None, redis=redis, principal=None)  # type: ignore[arg-type]
    return await get_consumer_lag(GetConsumerLagInput(consumer_group=group), ctx)


def _description() -> str:
    return next(t.description for t in list_tools() if t.name == "get_consumer_lag")


# 1. The interval is the loop's own, and N is chosen from the loop's own jitter


def test_both_measured_groups_run_the_base_poll_loop_unmodified() -> None:
    """`poll_interval_seconds` claims the cadence each group really runs at. Both groups the pass
    measures use `BaseKafkaConsumer.run()` as it is, so one set of loop numbers is the truth for
    both — a subclass that overrode `run()` would need its own interval."""
    assert JobDispatcherConsumer.run is BaseKafkaConsumer.run
    assert DependencyResolver.run is BaseKafkaConsumer.run
    for group in LIVE_REFRESHED_GROUPS:
        assert core.poll_interval_seconds(group) == core.POLL_INTERVAL_SECONDS


def test_the_interval_is_the_fetch_bound_and_n_clears_the_loops_worst_healthy_cycle() -> None:
    """The base loop asks Kafka, waits up to POLL_FETCH_WAIT_SECONDS for messages, and is cut off
    at POLL_FETCH_BOUND_SECONDS however Kafka behaves; a failed batch or a loop error adds
    POLL_RETRY_PAUSE_SECONDS before the next ask. So a running consumer's gap between asks is
    ~0.5 s idle and at most bound + pause (+ handling one batch). N is chosen so that worst
    healthy gap fits three times inside the line — a running group cannot cross it on jitter."""
    assert core.POLL_FETCH_WAIT_SECONDS == 0.5
    assert core.POLL_FETCH_BOUND_SECONDS == 2.0
    assert core.POLL_RETRY_PAUSE_SECONDS == 1.0
    assert core.POLL_INTERVAL_SECONDS == core.POLL_FETCH_BOUND_SECONDS
    assert core.POLLING_INTERVALS == 5
    assert _threshold() == 10.0

    worst_healthy_gap = core.POLL_FETCH_BOUND_SECONDS + core.POLL_RETRY_PAUSE_SECONDS
    assert _threshold() >= 3 * worst_healthy_gap
    # And the line is far below the INC-008 reading it exists for (39 s since the last poll,
    # 3 s since the measurement) and below Kafka's own five-minute eviction.
    assert _threshold() < 39 - 3
    assert _threshold() < Settings(environment="test").kafka_max_poll_interval_ms / 1000


async def test_the_loop_polls_with_the_numbers_the_verdict_assumes() -> None:
    """No mirror: `run()` reads its fetch wait and fetch bound from the constants the verdict is
    built on, so the two cannot drift apart."""

    class _Probe(BaseKafkaConsumer):
        async def handle_message(self, *_a: Any, **_kw: Any) -> None:
            return None

    c = _Probe(topics=["job.completed"], group_id=DEPENDENCY_RESOLVER_GROUP)
    seen: dict[str, Any] = {}

    async def _getmany(**kw: Any) -> dict[Any, Any]:
        seen["timeout_ms"] = kw.get("timeout_ms")
        c._running = False
        return {}

    real_wait_for = asyncio.wait_for

    async def _wait_for(aw: Any, timeout: float | None = None) -> Any:
        seen["bound"] = timeout
        return await real_wait_for(aw, timeout=timeout)

    fake = MagicMock()
    fake.getmany = _getmany
    c._consumer = fake
    c._running = True
    with patch.object(kafka_consumer_module.asyncio, "wait_for", _wait_for):
        await c.run()

    assert seen["timeout_ms"] == round(core.POLL_FETCH_WAIT_SECONDS * 1000)
    assert seen["bound"] == core.POLL_FETCH_BOUND_SECONDS


# 2. The verdict: one function, in core


def test_the_verdict_is_true_within_n_intervals_false_beyond_and_null_when_unknown() -> None:
    t0 = datetime(2026, 10, 10, 17, 0, tzinfo=UTC)
    line = _threshold()

    def verdict(gap: float) -> bool | None:
        return core.polling_verdict(
            t0, t0 + timedelta(seconds=gap), core.POLL_INTERVAL_SECONDS
        )

    assert verdict(0) is True
    assert verdict(0.5) is True
    assert verdict(line) is True, "the line itself still counts as polling"
    assert verdict(line + 0.001) is False
    assert verdict(36) is False, "the INC-008 reading"
    # A look stamped a hair before the poll (two clocks, one process) is not a negative gap.
    assert verdict(-1) is True
    # Unknown is not evidence.
    assert core.polling_verdict(None, t0, core.POLL_INTERVAL_SECONDS) is None
    assert core.polling_verdict(t0, None, core.POLL_INTERVAL_SECONDS) is None
    assert core.polling_verdict(t0, t0, None) is None


# 3. Running: true across many reads, at the loop's own jitter


def _running_stamps(rng: random.Random, *, seconds: float, worst: bool = False) -> list[float]:
    """Poll times of a running consumer, from the base loop's own numbers: each ask waits up to
    the fetch bound, handles its batch, and sometimes pauses after a failure."""
    stamps = [0.0]
    while stamps[-1] < seconds:
        if worst:
            gap = (
                core.POLL_FETCH_BOUND_SECONDS
                + core.POLL_RETRY_PAUSE_SECONDS
                + 1.0  # a whole second handling one batch of ten
            )
        else:
            gap = rng.uniform(0.0, core.POLL_FETCH_BOUND_SECONDS) + rng.uniform(0.0, 1.0)
            if rng.random() < 0.2:
                gap += core.POLL_RETRY_PAUSE_SECONDS
        stamps.append(stamps[-1] + gap)
    return stamps


def _last_before(stamps: list[float], at: float) -> float | None:
    earlier = [s for s in stamps if s <= at]
    return earlier[-1] if earlier else None


@pytest.mark.parametrize("worst", [False, True], ids=["jittered", "every-gap-worst-case"])
async def test_a_running_consumer_reads_polling_true_on_every_read(worst: bool) -> None:
    """An hour of a running consumer, looked at 2,000 times at arbitrary moments — the pass's
    clock is not synchronised with the consumer's. Not one read may say false."""
    rng = random.Random(373)
    redis = _RedisStub()
    t0 = datetime(2026, 10, 10, 17, 0, tzinfo=UTC)
    stamps = _running_stamps(rng, seconds=3_600, worst=worst)

    verdicts = []
    for at in sorted(rng.uniform(1.0, 3_600.0) for _ in range(2_000)):
        polled = _last_before(stamps, at)
        assert polled is not None
        reading = await _observe(
            redis,
            DEPENDENCY_RESOLVER_GROUP,
            polled_at=t0 + timedelta(seconds=polled),
            observed_at=t0 + timedelta(seconds=at),
        )
        verdicts.append(reading.polling)

    assert verdicts.count(True) == len(verdicts) == 2_000


async def test_the_real_loop_reads_polling_true_through_a_failed_batch() -> None:
    """The shipped `run()` against a Kafka double: idle fetches, then a batch whose handler fails
    (the longest healthy gap — the retry pause). The platform looks every 50 ms throughout."""

    class _Failing(BaseKafkaConsumer):
        async def handle_message(self, *_a: Any, **_kw: Any) -> None:
            raise RuntimeError("handler failed")

    c = _Failing(topics=["job.completed"], group_id=DEPENDENCY_RESOLVER_GROUP)
    record = MagicMock(topic="job.completed", partition=0, offset=7, value={}, key=None)
    fetches = {"n": 0}

    async def _getmany(**kw: Any) -> dict[Any, Any]:
        fetches["n"] += 1
        if fetches["n"] == 2:
            return {MagicMock(): [record]}
        if fetches["n"] >= 3:
            c._running = False
        await _REAL_SLEEP(kw["timeout_ms"] / 1000)
        return {}

    fake = MagicMock()
    fake.getmany = _getmany
    c._consumer = fake
    c._running = True
    redis = _RedisStub()
    verdicts: list[bool | None] = []

    async def _platform_looks() -> None:
        while c._running:
            await dispatcher._publish_last_poll(redis, DEPENDENCY_RESOLVER_GROUP, c)
            verdicts.append((await read_lag(redis, DEPENDENCY_RESOLVER_GROUP)).polling)
            await _REAL_SLEEP(0.05)

    with (
        patch.object(kafka_consumer_module, "validate_schema", lambda *_a: None),
        patch(
            "app.workers.kafka_consumer.get_settings",
            return_value=Settings(chaos_enabled=False, environment="test"),
        ),
    ):
        await asyncio.gather(c.run(), _platform_looks())

    assert fetches["n"] >= 3, "the loop went round"
    assert len(verdicts) >= 20
    assert all(v is True for v in verdicts), verdicts


# 4. Killed: false within N intervals, and stays false


async def test_a_killed_consumer_reads_false_within_n_intervals_and_stays_false() -> None:
    rng = random.Random(8)
    redis = _RedisStub()
    t0 = datetime(2026, 10, 10, 17, 0, tzinfo=UTC)
    stamps = _running_stamps(rng, seconds=600)
    kill = 300.0
    last_poll = _last_before(stamps, kill)
    assert last_poll is not None
    line = _threshold()

    seen_false = False
    first_false: float | None = None
    # The platform looks every second from the kill for ten minutes.
    for step in range(600):
        at = kill + step
        reading = await _observe(
            redis,
            DEPENDENCY_RESOLVER_GROUP,
            polled_at=t0 + timedelta(seconds=last_poll),
            observed_at=t0 + timedelta(seconds=at),
        )
        # The poll time is a fact at every look; only the verdict moves.
        assert reading.last_poll_at == t0 + timedelta(seconds=last_poll)
        if at - last_poll <= line:
            assert reading.polling is True, at
        else:
            assert reading.polling is False, at
            if first_false is None:
                first_false = at
            seen_false = True
        if seen_false:
            assert reading.polling is False, "a dead consumer never flaps back to true"

    assert first_false is not None
    # Within N intervals of its last poll, give or take the one-second look.
    assert first_false - last_poll <= line + 1.0


async def test_one_pass_over_a_killed_resolver_says_false_and_the_dispatcher_true() -> None:
    """Through the shipped metrics pass: both measured groups carry the verdict and the cadence."""
    redis = _RedisStub()
    now = datetime.now(UTC)

    await _one_metrics_pass(
        redis,
        _consumer(lag=0, polled_at=now - timedelta(seconds=1)),
        _consumer(lag=0, polled_at=now - timedelta(seconds=39)),
    )

    resolver = await _tool(redis, DEPENDENCY_RESOLVER_GROUP)
    assert (resolver.lag, resolver.lag_known, resolver.source) == (0, True, "live")
    assert resolver.age_seconds is not None and resolver.age_seconds <= 2
    assert resolver.polling is False
    assert resolver.poll_interval_seconds == core.POLL_INTERVAL_SECONDS
    assert resolver.last_poll_age_seconds is not None
    assert resolver.last_poll_age_seconds >= 39

    healthy = await _tool(redis, LIVE_REFRESHED_GROUP)
    assert healthy.polling is True
    assert healthy.poll_interval_seconds == core.POLL_INTERVAL_SECONDS


@pytest.mark.parametrize("seconds_since_poll", [3, 8, 9])
async def test_one_pass_over_a_recently_polled_consumer_says_true(
    seconds_since_poll: int,
) -> None:
    redis = _RedisStub()
    now = datetime.now(UTC)

    await _one_metrics_pass(
        redis,
        _consumer(lag=0, polled_at=now),
        _consumer(lag=0, polled_at=now - timedelta(seconds=seconds_since_poll)),
    )

    assert (await _tool(redis, DEPENDENCY_RESOLVER_GROUP)).polling is True


async def test_the_pass_records_when_it_looked_beside_the_poll_time() -> None:
    """The verdict is decided from the pass's own look, on the clock that stamped the poll."""
    redis = _RedisStub()
    polled = datetime.now(UTC) - timedelta(seconds=20)
    before = datetime.now(UTC)

    await _one_metrics_pass(redis, _consumer(lag=None, polled_at=polled), None)

    stored = json.loads(redis.store[last_poll_key(LIVE_REFRESHED_GROUP)])
    assert datetime.fromisoformat(stored["last_poll_at"]) == polled
    observed = datetime.fromisoformat(stored["observed_at"])
    assert observed >= before
    # Fresh-or-absent, as before: the record lives three passes and then reads unknown.
    assert redis.ttls[last_poll_key(LIVE_REFRESHED_GROUP)] == core.lag_value_ttl_seconds()


async def test_a_reading_taken_long_after_the_pass_does_not_flip_a_healthy_verdict() -> None:
    """The reason the verdict is not drawn against the reader's clock: at the platform's default
    60 s pass a healthy consumer's `last_poll_age_seconds` reaches a minute between passes. The
    look was fine; the reading says so until the record expires into unknown."""
    redis = _RedisStub()
    now = datetime.now(UTC)
    looked = now - timedelta(seconds=55)

    reading = await _observe(
        redis,
        LIVE_REFRESHED_GROUP,
        polled_at=looked - timedelta(seconds=1),
        observed_at=looked,
    )

    assert reading.last_poll_age_seconds is not None
    assert reading.last_poll_age_seconds >= 55
    assert reading.polling is True


# 5. Killed, then the kill clears and the consumer comes back: true again


async def test_after_the_kill_clears_and_the_supervisor_restarts_it_the_verdict_is_true_again() -> (
    None
):
    """The shipped supervisor and the shipped poll loop, with Kafka and Redis doubled: a few
    polls, the kill, the platform looks 30 s later (false), the kill clears, the supervisor
    restarts the consumer, it polls again, and the next look says true."""

    class _Probe(BaseKafkaConsumer):
        async def handle_message(self, *_a: Any, **_kw: Any) -> None:
            return None

        async def start(self) -> None:
            self._consumer = fake
            self._running = True
            self._chaos_killed = False

        async def stop(self) -> None:
            self._running = False

    c = _Probe(topics=["job.completed"], group_id=DEPENDENCY_RESOLVER_GROUP)
    fake = MagicMock()
    c._consumer = fake
    c._running = True
    redis = _RedisStub()
    phase = {"killed": False, "restarted": False, "polls_after_restart": 0}
    verdicts: dict[str, bool | None] = {}

    async def _getmany(**_kw: Any) -> dict[Any, Any]:
        if phase["restarted"]:
            phase["polls_after_restart"] += 1
            if phase["polls_after_restart"] >= 3:
                c._running = False  # an orderly stop ends supervision
        await _REAL_SLEEP(0.001)
        return {}

    fake.getmany = _getmany
    lookups = {"loop": 0, "strict": 0}

    async def _kill_seen_by_the_loop(_group: str) -> bool:
        lookups["loop"] += 1
        if lookups["loop"] == 4 and not phase["restarted"]:
            phase["killed"] = True
            return True
        return False

    async def _kill_seen_by_the_supervisor(_group: str) -> bool:
        lookups["strict"] += 1
        if lookups["strict"] == 1:
            # The platform looks 30 s after the consumer's last poll, while it is still down.
            frozen = c.last_poll_at
            assert frozen is not None
            looked = await _observe(
                redis,
                DEPENDENCY_RESOLVER_GROUP,
                polled_at=frozen,
                observed_at=frozen + timedelta(seconds=30),
            )
            verdicts["while_killed"] = looked.polling
            return True
        phase["restarted"] = True  # the flag is cleared; the supervisor restarts it
        return False

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "app.workers.kafka_consumer.get_settings",
                return_value=Settings(chaos_enabled=True, environment="test"),
            )
        )
        stack.enter_context(
            patch("app.workers.kafka_consumer._check_chaos_kill", _kill_seen_by_the_loop)
        )
        stack.enter_context(
            patch(
                "app.workers.kafka_consumer._check_chaos_latency",
                AsyncMock(return_value=0),
            )
        )
        stack.enter_context(
            patch(
                "app.workers.dispatcher._check_chaos_kill_strict",
                _kill_seen_by_the_supervisor,
            )
        )
        stack.enter_context(patch("asyncio.sleep", new=AsyncMock()))
        await asyncio.wait_for(dispatcher._supervise_consumer(c), timeout=5)

    assert phase["killed"] and phase["restarted"]
    assert verdicts["while_killed"] is False
    assert phase["polls_after_restart"] >= 3

    await dispatcher._publish_last_poll(redis, DEPENDENCY_RESOLVER_GROUP, c)
    back = await read_lag(redis, DEPENDENCY_RESOLVER_GROUP)
    assert back.polling is True


# 6. Unknown is not evidence: null where the platform holds no poll time


@pytest.mark.parametrize("group", STATIC_LAG_GROUPS)
async def test_a_static_group_carries_neither_field(group: str) -> None:
    """Nothing polls a recorded constant — even a stray record is not read for one."""
    now = datetime.now(UTC)
    redis = _RedisStub({core.lag_key(group): "15000"})
    await core.record_last_poll(redis, now, group=group, observed_at=now)

    reading = await _tool(redis, group)

    assert reading.source == "static"
    assert reading.polling is None
    assert reading.poll_interval_seconds is None


async def test_an_unrecognised_group_carries_neither_field() -> None:
    reading = await _tool(_RedisStub(), "no-such-consumer")

    assert reading.source == "unrecognized"
    assert reading.polling is None
    assert reading.poll_interval_seconds is None


@pytest.mark.parametrize("group", LIVE_REFRESHED_GROUPS)
async def test_a_live_group_with_no_poll_on_record_is_unknown_not_false(group: str) -> None:
    """Never polled, or the pass stopped long enough for the record to expire: the cadence is
    still known, the verdict is not."""
    reading = await _tool(_RedisStub(), group)

    assert reading.last_poll_at is None
    assert reading.polling is None
    assert reading.poll_interval_seconds == core.POLL_INTERVAL_SECONDS


async def test_a_record_without_the_look_time_is_unknown_not_a_guess() -> None:
    """A poll time with no record of when the platform looked (the v0.6.24 shape, which a worker
    mid-upgrade can still leave for a pass or two) gives the fact, not a verdict."""
    polled = datetime.now(UTC) - timedelta(seconds=120)
    redis = _RedisStub({last_poll_key(DEPENDENCY_RESOLVER_GROUP): polled.isoformat()})

    reading = await _tool(redis, DEPENDENCY_RESOLVER_GROUP)

    assert reading.last_poll_at == polled
    assert reading.polling is None


@pytest.mark.parametrize(
    "raw",
    ["not-a-time", "{}", '{"last_poll_at": "nope", "observed_at": "nope"}', "[1, 2]"],
)
async def test_an_unreadable_record_is_unknown(raw: str) -> None:
    redis = _RedisStub({last_poll_key(DEPENDENCY_RESOLVER_GROUP): raw})

    reading = await _tool(redis, DEPENDENCY_RESOLVER_GROUP)

    assert reading.last_poll_at is None
    assert reading.last_poll_age_seconds is None
    assert reading.polling is None


async def test_a_consumer_that_never_polled_leaves_no_record_and_reads_unknown() -> None:
    redis = _RedisStub()

    await _one_metrics_pass(
        redis, _consumer(lag=None, polled_at=None), _consumer(lag=None, polled_at=None)
    )

    for group in LIVE_REFRESHED_GROUPS:
        assert last_poll_key(group) not in redis.store
        assert (await _tool(redis, group)).polling is None


# 7. The wire: two output fields, and one sentence an agent cannot misread


def test_the_output_gains_exactly_the_two_fields() -> None:
    schema = GetConsumerLagOutput.model_json_schema()
    props = schema["properties"]
    assert {"polling", "poll_interval_seconds"} <= set(props)
    # Optional, default null: a caller's model that forbids extras needs to know, nothing more.
    assert "polling" not in schema.get("required", [])
    assert "poll_interval_seconds" not in schema.get("required", [])
    polling = props["polling"]["description"]
    assert "null" in polling and "unknown" in polling
    assert str(core.POLLING_INTERVALS) in polling
    interval = props["poll_interval_seconds"]["description"]
    assert "null" in interval


def test_the_description_states_the_verdict_in_one_sentence() -> None:
    description = _description()
    assert POLLING_SENTENCE in description
    assert "`polling: null`" in description, "unknown must be said to be unknown"


def test_the_description_was_trimmed_not_grown() -> None:
    assert len(_description()) < V0_6_24_DESCRIPTION_CHARS


def test_the_description_no_longer_asks_the_reader_to_threshold_the_age() -> None:
    """The v0.6.24 text left the comparison to the reader ("never more than a few seconds above
    `age_seconds`") — the very arithmetic the planner got wrong. The verdict replaces it."""
    description = _description()
    assert "a few seconds above" not in description
    assert "IS ANYTHING CONSUMING" not in description
    assert description.count("`polling") >= 2
