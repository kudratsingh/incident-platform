"""The stuck chain's cause is readable: the resolver's lag and the control loops' state.

WO-R3-371 (INC-008, ADR 0041). The `resolver_stall` world is `kill_consumer(dependency-resolver)`
plus `pause_control_loop(resume_unblocked_waiting)`, and before this order no tool the agent holds
could read either half: `get_consumer_lag("dependency-resolver")` answered `source: unrecognized`,
and nothing reported the loops. Two readings now exist, and this file is the worker-side half of
them — what the worker writes, and that it writes it on the pass it claims to. The wire half (the
tool responses, the scopes, the withheld audit rows) is
`tests/api/test_mcp_resolver_and_control_loops.py`.

Three things are pinned here because each is easy to get subtly wrong:

* **A stopped consumer's lag can be flat.** The resolver consumes `job.completed`; behind a quiet
  topic a dead resolver reads the same lag as a healthy one. So the metrics pass also records when
  each measured consumer last POLLED — an in-process stamp, published by the pass, so the consumer's
  own poll loop stays free of I/O — and that is the field that moves when the resolver is killed.
* **A paused loop must not stamp a pass.** The loop keeps turning while it skips its work, so a
  stamp taken before the pause check would report a loop that runs; the stamp goes after the work.
* **Paging is a separate decision.** The resolver is measured, not paged: `consumer_stalled` still
  evaluates worker-dispatcher only (ADR 0041 says why).
"""

from __future__ import annotations

import ast
import asyncio
import fnmatch
import json
import pathlib
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import Settings, get_settings
from app.core.consumer_lag import (
    DEPENDENCY_RESOLVER_GROUP,
    LIVE_REFRESHED_GROUP,
    LIVE_REFRESHED_GROUPS,
    SEEDED_CONSUMER_GROUPS,
    STATIC_LAG_GROUPS,
    lag_key,
    lag_value_ttl_seconds,
    last_poll_key,
    read_lag,
    record_last_poll,
    samples_key,
    source_for,
)
from app.core.loop_heartbeat import (
    LOOP_RUN_TTL_SECONDS,
    loop_run_key,
    read_loop_run,
    record_loop_run,
)
from app.core.outbox_heartbeat import RELAY_TICK_KEY
from app.mcp.registry import ToolContext
from app.mcp.tools.consumer_lag import GetConsumerLagInput, get_consumer_lag
from app.services import alert_rules
from app.workers import dispatcher
from app.workers.control_loop_pause import (
    LOOP_FUNCTIONS,
    ControlLoopName,
    loop_pause_state,
    pause_key_for,
)
from app.workers.dependency_resolver import DependencyResolver
from app.workers.kafka_consumer import BaseKafkaConsumer

_DISPATCHER_TREE = ast.parse(
    pathlib.Path(dispatcher.__file__).read_text(encoding="utf-8")
)


class _RedisStub:
    """get / set / ttl — every call either side of these two readings makes."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.store: dict[str, str] = dict(values or {})
        self.ttls: dict[str, int | None] = {}
        self.fail = False

    async def get(self, key: str) -> str | None:
        if self.fail:
            raise ConnectionError("redis unreachable")
        return self.store.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        if self.fail:
            raise ConnectionError("redis unreachable")
        self.store[key] = str(value)
        self.ttls[key] = ex
        return True

    async def ttl(self, key: str) -> int:
        if self.fail:
            raise ConnectionError("redis unreachable")
        if key not in self.store:
            return -2
        ttl = self.ttls.get(key)
        return -1 if ttl is None else ttl


def _consumer(*, lag: int | None, polled_at: datetime | None) -> MagicMock:
    c = MagicMock()
    c.in_flight = set()
    c.consumer_lag = AsyncMock(return_value=lag)
    c.last_poll_at = polled_at
    return c


async def _one_metrics_pass(
    redis: _RedisStub, dispatcher_consumer: Any, resolver: Any | None
) -> AsyncMock:
    """Exactly one iteration of the shipped `_metrics_loop`; returns the loop-stamp mock."""
    calls = {"n": 0}

    async def _sleep_once(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    stamp = AsyncMock()
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
        stack.enter_context(patch("app.workers.dispatcher.record_loop_run", stamp))
        await dispatcher._metrics_loop(
            redis, dispatcher_consumer, MagicMock(), resolver
        )
    return stamp


async def _lag(redis: _RedisStub, group: str) -> Any:
    ctx = ToolContext(db=None, redis=redis, principal=None)  # type: ignore[arg-type]
    return await get_consumer_lag(GetConsumerLagInput(consumer_group=group), ctx)


# 1. `dependency-resolver` is a live group, measured by the same pass as worker-dispatcher


def test_the_resolver_is_a_live_group_and_is_advertised() -> None:
    assert source_for(DEPENDENCY_RESOLVER_GROUP) == "live"
    assert LIVE_REFRESHED_GROUPS == (LIVE_REFRESHED_GROUP, DEPENDENCY_RESOLVER_GROUP)
    assert DEPENDENCY_RESOLVER_GROUP in SEEDED_CONSUMER_GROUPS
    # The seven recorded constants are exactly what they were.
    assert len(STATIC_LAG_GROUPS) == 7
    assert DEPENDENCY_RESOLVER_GROUP not in STATIC_LAG_GROUPS


def test_the_constant_names_the_group_the_resolver_actually_joins() -> None:
    """The reader keys on the constant and the consumer joins the group settings name; if
    the two drifted, every reading would be `lag_known: false` with nothing to say why."""
    assert get_settings().kafka_consumer_group_dependency == DEPENDENCY_RESOLVER_GROUP
    assert get_settings().kafka_consumer_group_worker == LIVE_REFRESHED_GROUP


async def test_one_pass_records_the_resolvers_lag_exactly_as_it_records_the_dispatchers() -> (
    None
):
    """The fresh-stack reading: lag known, source live, a dated sample, a fresh poll."""
    redis = _RedisStub()
    now = datetime.now(UTC)

    await _one_metrics_pass(
        redis,
        _consumer(lag=7, polled_at=now),
        _consumer(lag=3, polled_at=now),
    )

    # The value key: same shape and TTL as the dispatcher's (three passes).
    assert redis.store[lag_key(DEPENDENCY_RESOLVER_GROUP)] == "3"
    assert redis.ttls[lag_key(DEPENDENCY_RESOLVER_GROUP)] == lag_value_ttl_seconds()
    window = json.loads(redis.store[samples_key(DEPENDENCY_RESOLVER_GROUP)])
    assert [s["lag"] for s in window] == [3]
    # The dispatcher's own reading is untouched by the second group.
    assert redis.store[lag_key(LIVE_REFRESHED_GROUP)] == "7"

    reading = await _lag(redis, DEPENDENCY_RESOLVER_GROUP)
    assert reading.lag == 3
    assert reading.lag_known is True
    assert reading.source == "live"
    assert reading.age_seconds is not None and reading.age_seconds <= 2
    assert [s.lag for s in reading.recent_samples] == [3]
    assert reading.last_poll_at is not None
    assert reading.last_poll_age_seconds is not None
    assert reading.last_poll_age_seconds <= 2


async def test_a_killed_resolver_reads_as_not_polling_while_its_lag_stays_flat() -> None:
    """The INC-008 world. A killed consumer stops asking Kafka for messages, so its poll stamp
    freezes at the kill; behind a quiet `job.completed` topic its lag is a flat, fresh, KNOWN
    number. `last_poll_at` / `last_poll_age_seconds` are the fields that move — nothing else."""
    redis = _RedisStub()
    now = datetime.now(UTC)
    killed_at = now - timedelta(seconds=140)

    await _one_metrics_pass(
        redis,
        _consumer(lag=0, polled_at=now),
        _consumer(lag=0, polled_at=killed_at),
    )
    reading = await _lag(redis, DEPENDENCY_RESOLVER_GROUP)

    # Unchanged from a healthy idle resolver: this is why lag alone could not separate them.
    assert (reading.lag, reading.lag_known, reading.source) == (0, True, "live")
    assert reading.age_seconds is not None and reading.age_seconds <= 2
    # What moves.
    assert reading.last_poll_at == killed_at
    assert reading.last_poll_age_seconds is not None
    assert reading.last_poll_age_seconds >= 140
    assert reading.last_poll_age_seconds - reading.age_seconds >= 100

    # And the healthy dispatcher beside it does not read that way.
    healthy = await _lag(redis, LIVE_REFRESHED_GROUP)
    assert healthy.last_poll_age_seconds is not None
    assert healthy.last_poll_age_seconds <= 2


async def test_the_poll_record_is_written_even_when_the_lag_is_unknown() -> None:
    """After Kafka evicts a long-stopped consumer its lag reads unknown; the pass still
    publishes the poll stamp, so the reading says unknown lag AND an old poll."""
    redis = _RedisStub()
    stopped_at = datetime.now(UTC) - timedelta(seconds=600)

    await _one_metrics_pass(
        redis,
        _consumer(lag=None, polled_at=datetime.now(UTC)),
        _consumer(lag=None, polled_at=stopped_at),
    )
    reading = await _lag(redis, DEPENDENCY_RESOLVER_GROUP)

    assert reading.lag is None
    assert reading.lag_known is False
    assert reading.last_poll_at == stopped_at
    assert reading.last_poll_age_seconds is not None
    assert reading.last_poll_age_seconds >= 600
    assert lag_key(DEPENDENCY_RESOLVER_GROUP) not in redis.store, "unknown is never 0"
    assert redis.ttls[last_poll_key(DEPENDENCY_RESOLVER_GROUP)] == lag_value_ttl_seconds()


async def test_a_consumer_that_never_polled_publishes_no_poll_time() -> None:
    redis = _RedisStub()

    await _one_metrics_pass(
        redis,
        _consumer(lag=None, polled_at=None),
        _consumer(lag=None, polled_at=None),
    )

    assert last_poll_key(DEPENDENCY_RESOLVER_GROUP) not in redis.store
    reading = await _lag(redis, DEPENDENCY_RESOLVER_GROUP)
    assert reading.last_poll_at is None
    assert reading.last_poll_age_seconds is None


async def test_a_pass_with_no_resolver_still_measures_the_dispatcher() -> None:
    """The fourth argument is optional: the integration tier and older callers hand the loop
    one consumer, and that must keep working byte-for-byte."""
    redis = _RedisStub()

    await _one_metrics_pass(redis, _consumer(lag=5, polled_at=None), None)

    assert redis.store[lag_key(LIVE_REFRESHED_GROUP)] == "5"
    assert lag_key(DEPENDENCY_RESOLVER_GROUP) not in redis.store


def test_worker_loop_hands_the_resolver_to_the_metrics_pass() -> None:
    """Static: the resolver is only measured if `worker_loop` passes it in."""
    worker_loop = next(
        node
        for node in _DISPATCHER_TREE.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "worker_loop"
    )
    metrics_calls = [
        node
        for node in ast.walk(worker_loop)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "_metrics_loop"
    ]
    assert len(metrics_calls) == 1
    passed = [getattr(a, "id", None) for a in metrics_calls[0].args]
    assert "dep_resolver" in passed


async def test_a_static_group_never_reports_a_poll_time() -> None:
    """Nothing polls a recorded constant, so even a stray key is not read for one."""
    stray = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    redis = _RedisStub(
        {lag_key("billing-consumer"): "15000", last_poll_key("billing-consumer"): stray}
    )

    reading = await read_lag(redis, "billing-consumer")

    assert reading.source == "static"
    assert reading.last_poll_at is None
    assert reading.last_poll_age_seconds is None


async def test_an_unreadable_poll_time_is_unknown_not_a_guess() -> None:
    redis = _RedisStub({last_poll_key(DEPENDENCY_RESOLVER_GROUP): "not-a-time"})

    reading = await read_lag(redis, DEPENDENCY_RESOLVER_GROUP)

    assert reading.last_poll_at is None
    assert reading.last_poll_age_seconds is None


async def test_the_poll_writer_dates_in_utc_and_lives_three_passes() -> None:
    redis = _RedisStub()
    at = datetime(2026, 10, 10, 7, 0, 0, tzinfo=UTC)

    await record_last_poll(redis, at, group=DEPENDENCY_RESOLVER_GROUP)

    assert redis.store[last_poll_key(DEPENDENCY_RESOLVER_GROUP)] == at.isoformat()
    assert redis.ttls[last_poll_key(DEPENDENCY_RESOLVER_GROUP)] == lag_value_ttl_seconds()
    assert last_poll_key(DEPENDENCY_RESOLVER_GROUP).startswith("kafka:consumer_lag:")


def test_the_lag_rule_still_pages_on_the_dispatcher_only() -> None:
    """Measured is not paged (ADR 0041): a completion burst after a recovery can put a
    momentary spike in the resolver's lag, and paging on it is a decision of its own."""
    assert alert_rules._measured_groups() == (LIVE_REFRESHED_GROUP,)


# 2. The consumer's own half: an in-process poll stamp, and lag on every consumer


class _Probe(BaseKafkaConsumer):
    async def handle_message(self, *_a: Any, **_kw: Any) -> None:
        return None


async def test_every_poll_stamps_the_consumer_and_does_no_io_to_do_it() -> None:
    c = _Probe(topics=["job.completed"], group_id=DEPENDENCY_RESOLVER_GROUP)
    polls = {"n": 0}

    async def _getmany(**_kw: Any) -> dict[Any, Any]:
        polls["n"] += 1
        if polls["n"] >= 2:
            c._running = False
        return {}

    fake = MagicMock()
    fake.getmany = _getmany
    c._consumer = fake
    c._running = True
    before = datetime.now(UTC)

    await c.run()

    assert polls["n"] == 2
    assert c.last_poll_at is not None
    assert c.last_poll_at >= before


async def test_a_killed_consumer_stops_stamping() -> None:
    c = _Probe(topics=["job.completed"], group_id=DEPENDENCY_RESOLVER_GROUP)
    frozen = datetime.now(UTC) - timedelta(seconds=60)
    c.last_poll_at = frozen
    fake = MagicMock()
    fake.getmany = AsyncMock(return_value={})
    c._consumer = fake
    c._running = True

    with patch(
        "app.workers.kafka_consumer.get_settings",
        return_value=Settings(chaos_enabled=True, environment="test"),
    ), patch(
        "app.workers.kafka_consumer._check_chaos_kill", AsyncMock(return_value=True)
    ):
        await c.run()

    assert c.chaos_killed is True
    fake.getmany.assert_not_awaited()
    assert c.last_poll_at == frozen, "a consumer that did not poll must not look like one that did"


async def test_the_resolver_can_report_its_own_lag() -> None:
    """`consumer_lag()` moved to the base class: the same arithmetic, the same unknown-is-None
    rule and the same ADR 0040 bounds, for any consumer the pass is handed."""
    r = DependencyResolver(MagicMock())
    assert await r.consumer_lag() is None, "not started is unknown, never 0"

    kafka = AsyncMock()
    kafka.assignment = lambda: {"tp"}
    kafka.end_offsets = AsyncMock(return_value={"tp": 12})
    kafka.committed = AsyncMock(return_value=9)
    r._consumer = kafka
    assert await r.consumer_lag() == 3

    r.restarting = True
    assert await r.consumer_lag() is None


# 3. The loops record each completed pass


async def test_a_loop_run_is_recorded_outside_the_lab_namespace() -> None:
    redis = _RedisStub()
    at = datetime(2026, 10, 10, 7, 0, 0, tzinfo=UTC)

    await record_loop_run(ControlLoopName.RESUME_UNBLOCKED_WAITING, redis, now=at)

    key = loop_run_key(ControlLoopName.RESUME_UNBLOCKED_WAITING)
    assert key == "worker:loop:resume_unblocked_waiting:last_run"
    assert redis.store[key] == at.isoformat()
    assert redis.ttls[key] == LOOP_RUN_TTL_SECONDS
    assert await read_loop_run(redis, ControlLoopName.RESUME_UNBLOCKED_WAITING) == at


async def test_a_lost_loop_stamp_never_costs_the_pass() -> None:
    redis = _RedisStub()
    redis.fail = True

    # Must not raise: the loop exists to move work, and a diagnostic must never stop it.
    await record_loop_run(ControlLoopName.METRICS, redis)


def test_the_relay_has_one_record_not_two() -> None:
    """The relay already stamps every pass (ADR 0028); its loop reading IS that stamp."""
    assert loop_run_key(ControlLoopName.OUTBOX_RELAY) == RELAY_TICK_KEY


@pytest.mark.parametrize("member", sorted(ControlLoopName, key=lambda m: m.value))
def test_the_reset_never_sweeps_a_loop_record(member: ControlLoopName) -> None:
    """A loop record describes the platform, not a world someone arranged, so the reset's one
    `chaos:*` scan must not clear it — and its name must not hint at why a loop stopped."""
    from tests.unit.test_eval_reset import _reset_module

    patterns = _reset_module()._CHAOS_KEY_PATTERNS
    key = loop_run_key(member)
    assert not key.startswith("chaos:")
    assert not any(fnmatch.fnmatch(key, p) for p in patterns)


def _calls_in(func_name: str, callee: str) -> list[ast.Call]:
    func = next(
        node
        for node in _DISPATCHER_TREE.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == func_name
    )
    return [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == callee
    ]


@pytest.mark.parametrize(
    ("member", "loop_name"),
    sorted(
        (m.name, fn)
        for m, fn in LOOP_FUNCTIONS.items()
        if m is not ControlLoopName.OUTBOX_RELAY
    ),
)
def test_each_loop_records_its_own_pass_after_its_pause_check(
    member: str, loop_name: str
) -> None:
    """Static, like the pause bijection: every loop stamps its own member and no other, and
    the stamp comes after `loop_is_paused` in statement order — a paused loop keeps turning,
    and a stamp in front of the check would report it running."""
    stamps = _calls_in(loop_name, "record_loop_run")
    stamped = {
        a.attr
        for call in stamps
        for a in call.args[:1]
        if isinstance(a, ast.Attribute) and getattr(a.value, "id", None) == "ControlLoopName"
    }
    assert stamped == {member}, f"{loop_name} stamps {stamped}"

    pause_line = min(c.lineno for c in _calls_in(loop_name, "loop_is_paused"))
    assert all(c.lineno > pause_line for c in stamps), (
        f"{loop_name} stamps a pass before its pause check"
    )


def test_the_relay_stamps_inside_its_tick_not_around_the_loop() -> None:
    assert _calls_in("_outbox_relay_tick", "record_relay_tick")
    assert not _calls_in("_outbox_relay_loop", "record_loop_run")


async def test_a_paused_resume_sweep_records_no_pass_and_an_unpaused_one_does() -> None:
    for paused, expected in ((True, 0), (False, 1)):
        stamp = AsyncMock()
        once = AsyncMock(return_value=None)
        with patch(
            "app.workers.dispatcher.loop_is_paused",
            AsyncMock(side_effect=[paused, asyncio.CancelledError()]),
        ), patch(
            "app.workers.dispatcher._resume_unblocked_waiting_once", once
        ), patch("app.workers.dispatcher.record_loop_run", stamp), patch(
            "asyncio.sleep", AsyncMock()
        ):
            await dispatcher._resume_unblocked_waiting_loop(MagicMock(), AsyncMock())

        assert stamp.await_count == expected, f"paused={paused}"
        if expected:
            stamp.assert_awaited_once_with(ControlLoopName.RESUME_UNBLOCKED_WAITING)


async def test_the_metrics_pass_records_its_own_run() -> None:
    redis = _RedisStub()
    stamp = await _one_metrics_pass(redis, _consumer(lag=1, polled_at=None), None)
    stamp.assert_awaited_once_with(ControlLoopName.METRICS)


# 4. The pause reading: the same predicate the loop decides on


async def test_the_pause_reading_matches_the_loops_own_check() -> None:
    redis = _RedisStub({pause_key_for(ControlLoopName.RESUME_UNBLOCKED_WAITING): "paused"})
    redis.ttls[pause_key_for(ControlLoopName.RESUME_UNBLOCKED_WAITING)] = 420
    lab_on = Settings(chaos_enabled=True, environment="test")

    with patch("app.workers.control_loop_pause.get_settings", return_value=lab_on):
        assert await loop_pause_state(
            redis, ControlLoopName.RESUME_UNBLOCKED_WAITING
        ) == (True, 420)
        assert await loop_pause_state(redis, ControlLoopName.OUTBOX_RELAY) == (
            False,
            None,
        )

    # With the flag off the loop never honours the key, so the reading never reports it.
    with patch(
        "app.workers.control_loop_pause.get_settings",
        return_value=Settings(chaos_enabled=False, environment="test"),
    ):
        assert await loop_pause_state(
            redis, ControlLoopName.RESUME_UNBLOCKED_WAITING
        ) == (False, None)


async def test_the_pause_reading_raises_where_the_loop_fails_open() -> None:
    """The loop fails open (a lost check must not stop work). A READER that failed open would
    say "not paused" about a pause it could not see — so it raises, and the tool says unknown."""
    redis = _RedisStub()
    redis.fail = True
    with patch(
        "app.workers.control_loop_pause.get_settings",
        return_value=Settings(chaos_enabled=True, environment="test"),
    ), pytest.raises(ConnectionError):
        await loop_pause_state(redis, ControlLoopName.RESUME_UNBLOCKED_WAITING)


# 5. The decision is written down where the next reader looks


def test_adr_0041_exists_and_is_indexed() -> None:
    root = pathlib.Path(__file__).resolve().parents[3]
    adr = root / "docs" / "ADR" / "0041-the-stuck-chains-cause-is-readable.md"
    index = (root / "docs" / "ADR" / "README.md").read_text(encoding="utf-8")
    redis_doc = (root / "docs" / "REDIS.md").read_text(encoding="utf-8")

    assert adr.is_file(), "ADR 0041 is missing"
    assert adr.name in index, "ADR 0041 is not in docs/ADR/README.md"
    # Every key this order writes is in the key catalog (CLAUDE.md: REDIS.md lists every key).
    for key in (
        "kafka:consumer_lag:dependency-resolver",
        "kafka:consumer_lag:{group}:last_poll",
        "worker:loop:{loop}:last_run",
    ):
        assert key in redis_doc, f"{key} is not catalogued in docs/REDIS.md"


async def test_a_redis_outage_warns_once_a_minute_not_on_every_pass() -> None:
    """The fastest loops pass twice a second; an outage must not add four warnings a second."""
    from app.core import loop_heartbeat

    redis = _RedisStub()
    redis.fail = True
    loop_heartbeat._last_warned.pop("metrics", None)
    with patch.object(loop_heartbeat.logger, "warning") as warn:
        for _ in range(5):
            await record_loop_run(ControlLoopName.METRICS, redis)
    assert warn.call_count == 1
