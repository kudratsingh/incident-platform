"""Each process's pool gauge: the write, the read, and what an absent writer means.

WO-R3-289 closes WO-R3-219's D1. `saturate_db_pool` holds the worker's pool and every
`pool_*` field on the read surface described the MCP process's own, so the fault was real and
invisible (ADR 0030 § what this does not close). What these tests hold: a process that has
not published is **unknown with a reason**, never zero; an unreachable or unreadable store is
also unknown, never an empty listing that reads as "every pool is fine"; the record expires,
so a process that stops publishing drops out instead of freezing at its last healthy number;
the write fails open; and the key is a platform key, so the environment reset does not carry
it (ADR 0033).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.core.db_pool_stats import reset_pool_wait_timeouts
from app.core.pool_state import (
    POOL_GAUGES_UNKNOWN_NONE_PUBLISHED,
    POOL_GAUGES_UNKNOWN_UNREACHABLE,
    POOL_GAUGES_UNKNOWN_UNREADABLE,
    POOL_PROCESSES,
    POOL_STATE_KEY_PREFIX,
    POOL_STATE_REFRESH_INTERVAL_SECONDS,
    POOL_STATE_TTL_SECONDS,
    PROCESS_API_WORKER,
    PROCESS_MCP,
    pool_key_for,
    publish_pool_state,
    read_pool_states,
    record_process_pool,
    start_pool_gauge,
    stop_pool_gauge,
)


class _FakeRedis:
    """Enough Redis for one namespace: SET with an expiry, GET, SCAN."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}
        self.sets: int = 0

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self.values[key] = str(value)
        self.ttls[key] = ex
        self.sets += 1
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def scan(
        self, cursor: int, match: str = "*", count: int = 10
    ) -> tuple[int, list[str]]:
        prefix = match.rstrip("*")
        return 0, [k for k in self.values if k.startswith(prefix)]


class _BrokenRedis:
    """A store that cannot be reached at all."""

    async def set(self, *_a: Any, **_kw: Any) -> bool:
        raise ConnectionError("nothing is listening")

    async def get(self, *_a: Any, **_kw: Any) -> str | None:
        raise ConnectionError("nothing is listening")

    async def scan(self, *_a: Any, **_kw: Any) -> tuple[int, list[str]]:
        raise ConnectionError("nothing is listening")


class _FakeQueuePool:
    """A pool that reports its counters, which is all a gauge asks of one."""

    _max_overflow = 10

    def __init__(self, checked_out: int = 4, overflow: int = 2) -> None:
        self._checked_out = checked_out
        self._overflow = overflow

    def size(self) -> int:
        return 5

    def checkedout(self) -> int:
        return self._checked_out

    def overflow(self) -> int:
        return self._overflow


class _MutePool:
    """A pool kind that keeps no counters — SQLite's under the unit tier."""


@pytest.fixture(autouse=True)
def _clean_counter() -> Any:
    reset_pool_wait_timeouts()
    yield
    reset_pool_wait_timeouts()


# The two process names are a closed set


def test_the_processes_are_a_closed_two_member_set() -> None:
    """A third process has to declare itself here rather than inventing a name on the
    wire, because the reading's `process` field is what a caller keys on."""
    assert POOL_PROCESSES == (PROCESS_API_WORKER, PROCESS_MCP)


def test_a_key_is_only_built_for_a_declared_process() -> None:
    assert pool_key_for(PROCESS_MCP) == f"{POOL_STATE_KEY_PREFIX}{PROCESS_MCP}"
    with pytest.raises(ValueError):
        pool_key_for("nobody")


def test_the_key_is_not_in_the_lab_namespace() -> None:
    """`reset_eval_state.py` sweeps `chaos:*`. This is a platform key, so the reset does
    not carry it — which is the point: the gauge outlives a world reset (ADR 0033)."""
    for process in POOL_PROCESSES:
        assert not pool_key_for(process).startswith("chaos:")


# Nobody has published


async def test_no_writer_reads_as_unknown_with_a_reason_never_as_zero() -> None:
    """The failure that matters: a pool nobody reported must not read as an idle one."""
    records, unknown = await read_pool_states(_FakeRedis())

    assert records == ()
    assert unknown == POOL_GAUGES_UNKNOWN_NONE_PUBLISHED


async def test_an_unreachable_store_reads_as_unknown_not_as_all_pools_healthy() -> None:
    records, unknown = await read_pool_states(_BrokenRedis())

    assert records == ()
    assert unknown == POOL_GAUGES_UNKNOWN_UNREACHABLE


async def test_a_record_that_cannot_be_read_is_unknown_rather_than_skipped() -> None:
    redis = _FakeRedis()
    redis.values[pool_key_for(PROCESS_MCP)] = "{not json"

    records, unknown = await read_pool_states(redis)

    assert records == ()
    assert unknown == POOL_GAUGES_UNKNOWN_UNREADABLE


# One writer, one reader


async def test_a_pool_published_by_one_process_reads_back_whole() -> None:
    redis = _FakeRedis()
    at = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)

    await publish_pool_state(
        redis,
        process=PROCESS_API_WORKER,
        size=5,
        checked_out=14,
        overflow=9,
        max_overflow=10,
        wait_timeouts_1m=3,
        now=at,
    )
    records, unknown = await read_pool_states(redis)

    assert unknown is None
    assert len(records) == 1
    record = records[0]
    assert record.process == PROCESS_API_WORKER
    assert record.size == 5
    assert record.checked_out == 14
    assert record.overflow == 9
    assert record.max_overflow == 10
    assert record.wait_timeouts_1m == 3
    assert record.written_at == at


async def test_both_processes_are_listed_in_process_order() -> None:
    redis = _FakeRedis()
    for process in reversed(POOL_PROCESSES):
        await publish_pool_state(
            redis,
            process=process,
            size=5,
            checked_out=1,
            overflow=0,
            max_overflow=10,
            wait_timeouts_1m=0,
        )

    records, unknown = await read_pool_states(redis)

    assert unknown is None
    assert tuple(r.process for r in records) == POOL_PROCESSES


async def test_the_record_carries_a_ttl_so_a_silent_process_drops_out() -> None:
    """Without it a process that wedged would keep reporting its last healthy pool
    forever, which is worse than reporting nothing."""
    redis = _FakeRedis()
    await publish_pool_state(
        redis,
        process=PROCESS_MCP,
        size=5,
        checked_out=1,
        overflow=0,
        max_overflow=10,
        wait_timeouts_1m=0,
    )

    assert redis.ttls[pool_key_for(PROCESS_MCP)] == POOL_STATE_TTL_SECONDS


def test_the_ttl_is_several_cadences_and_not_a_day() -> None:
    """A breaker's record lives a day because its state is latched. A pool reading is a
    sample, so the TTL is a small multiple of the cadence that writes it."""
    assert POOL_STATE_TTL_SECONDS >= 3 * POOL_STATE_REFRESH_INTERVAL_SECONDS
    assert POOL_STATE_TTL_SECONDS <= 600


async def test_a_zero_checkout_is_published_as_a_measurement() -> None:
    """An idle pool is a reading. Only an absent record is an unknown."""
    redis = _FakeRedis()
    await publish_pool_state(
        redis,
        process=PROCESS_MCP,
        size=5,
        checked_out=0,
        overflow=0,
        max_overflow=10,
        wait_timeouts_1m=0,
    )

    records, unknown = await read_pool_states(redis)

    assert unknown is None
    assert records[0].checked_out == 0


async def test_a_stated_null_max_overflow_survives_the_round_trip() -> None:
    redis = _FakeRedis()
    await publish_pool_state(
        redis,
        process=PROCESS_MCP,
        size=5,
        checked_out=1,
        overflow=0,
        max_overflow=None,
        wait_timeouts_1m=0,
    )

    records, _ = await read_pool_states(redis)

    assert records[0].max_overflow is None


# The write fails open


async def test_a_write_that_cannot_reach_the_store_is_dropped_not_raised() -> None:
    """A diagnostic must never cost a caller its request (the breaker rule, ADR 0030)."""
    await publish_pool_state(
        _BrokenRedis(),
        process=PROCESS_MCP,
        size=5,
        checked_out=1,
        overflow=0,
        max_overflow=10,
        wait_timeouts_1m=0,
    )


async def test_a_record_written_without_a_timestamp_is_not_a_record() -> None:
    """`written_at` is what makes an age sayable, so a record missing it is unreadable
    rather than a reading with an invented time."""
    redis = _FakeRedis()
    redis.values[pool_key_for(PROCESS_MCP)] = json.dumps(
        {"process": PROCESS_MCP, "size": 5, "checked_out": 1}
    )

    records, unknown = await read_pool_states(redis)

    assert records == ()
    assert unknown == POOL_GAUGES_UNKNOWN_UNREADABLE


# Reading the process's own pool


async def test_a_pool_that_reports_counters_is_published() -> None:
    redis = _FakeRedis()

    assert await record_process_pool(
        _FakeQueuePool(), process=PROCESS_API_WORKER, redis=redis
    )

    records, _ = await read_pool_states(redis)
    assert records[0].checked_out == 4
    assert records[0].overflow == 2
    assert records[0].max_overflow == 10


async def test_a_pool_that_keeps_no_counters_publishes_nothing_at_all() -> None:
    """Absent, rather than a record of nulls: a reading nobody can fill is not a reading,
    and the tool says an absent process is not a claim about that process."""
    redis = _FakeRedis()

    assert not await record_process_pool(
        _MutePool(), process=PROCESS_MCP, redis=redis
    )

    records, unknown = await read_pool_states(redis)
    assert records == ()
    assert unknown == POOL_GAUGES_UNKNOWN_NONE_PUBLISHED


# The publisher task


async def test_the_publisher_writes_at_boot_and_then_on_its_cadence() -> None:
    """At boot, not one interval later: a process whose gauge appears only after the
    first tick is invisible for exactly as long as a scenario takes to arm a fault."""
    redis = _FakeRedis()
    pool = _FakeQueuePool()
    await start_pool_gauge(
        process=PROCESS_MCP,
        pool_getter=lambda: pool,
        redis=redis,
        interval=0.02,
    )
    try:
        for _ in range(200):
            if redis.sets >= 3:
                break
            await asyncio.sleep(0.01)
    finally:
        await stop_pool_gauge()

    assert redis.sets >= 3
    records, unknown = await read_pool_states(redis)
    assert unknown is None
    assert records[0].process == PROCESS_MCP


async def test_the_publisher_survives_a_store_that_is_down() -> None:
    """It logs and keeps its cadence — the alternative is a task that dies on the first
    Redis blip and a gauge that never comes back without a restart."""
    redis = _BrokenRedis()
    await start_pool_gauge(
        process=PROCESS_MCP,
        pool_getter=lambda: _FakeQueuePool(),
        redis=redis,
        interval=0.02,
    )
    try:
        await asyncio.sleep(0.1)
    finally:
        await stop_pool_gauge()


async def test_the_publisher_survives_a_pool_it_cannot_reach() -> None:
    """A getter that raises must not end the task either."""

    def _boom() -> Any:
        raise RuntimeError("no engine yet")

    redis = _FakeRedis()
    await start_pool_gauge(
        process=PROCESS_MCP, pool_getter=_boom, redis=redis, interval=0.02
    )
    try:
        await asyncio.sleep(0.1)
    finally:
        await stop_pool_gauge()

    assert redis.sets == 0


async def test_the_publisher_refuses_an_undeclared_process_at_boot() -> None:
    """Loud at boot rather than a gauge that quietly never appears."""
    with pytest.raises(ValueError):
        await start_pool_gauge(
            process="worker-2", pool_getter=lambda: _FakeQueuePool(), redis=_FakeRedis()
        )


async def test_starting_twice_keeps_one_publisher() -> None:
    redis = _FakeRedis()
    try:
        await start_pool_gauge(
            process=PROCESS_MCP,
            pool_getter=lambda: _FakeQueuePool(),
            redis=redis,
            interval=5.0,
        )
        await start_pool_gauge(
            process=PROCESS_MCP,
            pool_getter=lambda: _FakeQueuePool(),
            redis=redis,
            interval=5.0,
        )
        await asyncio.sleep(0.05)
    finally:
        await stop_pool_gauge()

    assert redis.sets == 1


async def test_stopping_a_publisher_that_never_started_is_a_no_op() -> None:
    await stop_pool_gauge()


# An old record is still a record, with its age sayable


async def test_an_old_record_keeps_its_own_timestamp() -> None:
    """The reader computes the age; the writer never restates one, because the two are on
    different clocks and only the reader knows when it read."""
    redis = _FakeRedis()
    long_ago = datetime.now(UTC) - timedelta(seconds=45)
    await publish_pool_state(
        redis,
        process=PROCESS_API_WORKER,
        size=5,
        checked_out=14,
        overflow=9,
        max_overflow=10,
        wait_timeouts_1m=7,
        now=long_ago,
    )

    records, _ = await read_pool_states(redis)

    assert records[0].written_at == long_ago
