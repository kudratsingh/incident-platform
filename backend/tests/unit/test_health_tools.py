"""`get_postgres_health`'s pool and query readings (WO-R3-217, plan v2.1 WP-8.1).

Family A needs three faults told apart from reads alone, and the reading it had was a ping
and a connection count. What these tests hold: every new field has a degraded form that is
null *with a reason* rather than a zero, because a zero and an unknown are different facts;
the two readings the plan asked for and Postgres cannot take say so in a stable sentence
(ADR 0030); the pool numbers name the process they describe; and checkout timeouts are
counted, because SQLAlchemy keeps no such number.
"""

from __future__ import annotations

import uuid
from typing import Any

import app.mcp.tools  # noqa: F401  — import for @tool registration side effects
import pytest
from app.core.db_pool_stats import (
    POOL_STATS_UNKNOWN_POOL_KIND,
    WAIT_TIMEOUT_WINDOW_SECONDS,
    CountingQueuePool,
    pool_wait_timeouts_in_window,
    read_pool_stats,
    record_pool_wait_timeout,
    reset_pool_wait_timeouts,
)
from app.core.scopes import Scope
from app.dependencies import Principal
from app.mcp.registry import ToolContext, get_tool
from app.mcp.tools.health import (
    QUERY_STATS_UNKNOWN_EXTENSION_ABSENT,
    QUERY_STATS_UNKNOWN_NO_WINDOWED_HISTORY,
    QUERY_STATS_UNKNOWN_NOT_POSTGRES,
    QUERY_STATS_UNKNOWN_REASONS,
    SLOW_QUERY_THRESHOLD_MS,
    PostgresHealthOutput,
    get_postgres_health,
)
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool


def _ctx(db: AsyncSession) -> ToolContext:
    return ToolContext(
        db=db,
        redis=object(),
        principal=Principal(
            kind="service_account",
            tenant_id=uuid.uuid4(),
            scopes=frozenset({Scope.TELEMETRY_READ.value}),
        ),
    )


async def _call(db: AsyncSession) -> PostgresHealthOutput:
    """Built from the registered input model, so the call goes through the contract."""
    definition = get_tool("get_postgres_health")
    assert definition is not None
    return await get_postgres_health(definition.input_model(), _ctx(db))


class _FakeQueuePool:
    """A pool that reports its counters, which is all `read_pool_stats` asks of one."""

    _max_overflow = 10

    def size(self) -> int:
        return 5

    def checkedout(self) -> int:
        return 4

    def overflow(self) -> int:
        return 2


@pytest.fixture(autouse=True)
def _clean_counter() -> Any:
    reset_pool_wait_timeouts()
    yield
    reset_pool_wait_timeouts()


# The reading on a non-Postgres database: unknown, with the reason that says which case


async def test_query_fields_are_null_with_the_not_postgres_reason_on_sqlite(
    db_session: AsyncSession,
) -> None:
    """A zero would claim there were no slow queries. There is no measurement at all."""
    out = await _call(db_session)

    assert out.ok is True
    assert out.dialect == "sqlite"
    assert out.p95_query_ms_1m is None
    assert out.slow_query_count_1m is None
    assert out.longest_active_query_ms is None
    assert out.active_queries_over_slow_threshold is None
    assert out.query_stats_unknown_reason == QUERY_STATS_UNKNOWN_NOT_POSTGRES


async def test_the_slow_query_threshold_is_reported_even_when_the_count_is_unknown(
    db_session: AsyncSession,
) -> None:
    """A count over a threshold nobody stated cannot be read. The yardstick always ships."""
    out = await _call(db_session)

    assert out.slow_query_threshold_ms == SLOW_QUERY_THRESHOLD_MS
    assert out.slow_query_threshold_ms > 0


async def test_pool_fields_are_null_with_a_reason_when_the_pool_kind_keeps_no_counters(
    db_session: AsyncSession,
) -> None:
    """The test engine runs a `StaticPool`, which counts nothing — so neither do we."""
    out = await _call(db_session)

    assert out.pool_checked_out is None
    assert out.pool_overflow is None
    assert out.pool_size is None
    assert out.pool_max_overflow is None
    assert out.pool_wait_timeouts_1m is None
    assert out.pool_stats_unknown_reason == POOL_STATS_UNKNOWN_POOL_KIND


async def test_the_unknown_reasons_are_a_closed_set(db_session: AsyncSession) -> None:
    """Three cases, three stable sentences: a caller may key on them."""
    assert QUERY_STATS_UNKNOWN_REASONS == (
        QUERY_STATS_UNKNOWN_NOT_POSTGRES,
        QUERY_STATS_UNKNOWN_EXTENSION_ABSENT,
        QUERY_STATS_UNKNOWN_NO_WINDOWED_HISTORY,
    )
    out = await _call(db_session)
    assert out.query_stats_unknown_reason in QUERY_STATS_UNKNOWN_REASONS


async def test_a_failed_probe_still_reports_the_shape_with_everything_unknown(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ok=false` is a report, not a different shape: no field becomes a confident zero."""

    async def _boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("driver said no")

    monkeypatch.setattr(db_session, "execute", _boom)
    out = await _call(db_session)

    assert out.ok is False
    assert out.error is not None
    assert out.pool_checked_out is None
    assert out.p95_query_ms_1m is None
    assert out.longest_active_query_ms is None
    assert out.query_stats_unknown_reason is not None


# `read_pool_stats` — the numbers, and the honest refusal


def test_read_pool_stats_reports_a_queue_pool_and_no_reason() -> None:
    stats, reason = read_pool_stats(_FakeQueuePool())

    assert reason is None
    assert stats is not None
    assert (stats.size, stats.checked_out, stats.overflow, stats.max_overflow) == (
        5,
        4,
        2,
        10,
    )


def test_a_pool_that_has_never_filled_reports_no_overflow_rather_than_a_negative() -> None:
    """`QueuePool.overflow()` starts at `-pool_size` and counts connections ever created
    minus that size, so a fresh pool answers -5. Under the name "overflow" that reads as
    five below zero instead of "none" — CI caught it on a live Postgres."""

    class _FreshPool:
        _max_overflow = 3

        def size(self) -> int:
            return 5

        def checkedout(self) -> int:
            return 0

        def overflow(self) -> int:
            return -5

    stats, reason = read_pool_stats(_FreshPool())

    assert reason is None
    assert stats is not None
    assert stats.overflow == 0


async def test_the_reading_counts_the_connection_the_call_itself_holds() -> None:
    """A session checks a connection out lazily, on its first statement, so a reading
    taken before the ping counts the pool as empty. That shipped once and CI found it:
    `pool_checked_out: 0` against a live Postgres while the call held a connection."""
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=CountingQueuePool, pool_size=5, max_overflow=2
    )
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                definition = get_tool("get_postgres_health")
                assert definition is not None
                out = await get_postgres_health(definition.input_model(), _ctx(session))
    finally:
        await engine.dispose()

    assert out.pool_stats_unknown_reason is None
    assert out.pool_checked_out == 1, "the reading missed its own connection"
    assert out.pool_overflow == 0
    assert out.pool_size == 5
    assert out.pool_max_overflow == 2


def test_read_pool_stats_refuses_a_pool_that_reports_nothing() -> None:
    stats, reason = read_pool_stats(StaticPool(creator=lambda: None))

    assert stats is None
    assert reason == POOL_STATS_UNKNOWN_POOL_KIND


def test_read_pool_stats_refuses_when_there_is_no_pool_at_all() -> None:
    stats, reason = read_pool_stats(None)

    assert stats is None
    assert reason == POOL_STATS_UNKNOWN_POOL_KIND


# Checkout timeouts — the one pool number SQLAlchemy does not keep


def test_the_wait_timeout_window_only_counts_the_last_minute() -> None:
    """A rolling window, so a burst an hour ago does not read as a burst now."""
    record_pool_wait_timeout(now=1_000.0)
    record_pool_wait_timeout(now=1_000.5)

    assert pool_wait_timeouts_in_window(now=1_001.0) == 2
    assert pool_wait_timeouts_in_window(now=1_000.0 + WAIT_TIMEOUT_WINDOW_SECONDS + 1) == 0


def test_no_timeouts_reads_as_zero_not_as_unknown() -> None:
    """Here a zero is a real measurement: this process waited for nothing."""
    assert pool_wait_timeouts_in_window(now=5_000.0) == 0


async def test_the_counting_pool_records_a_real_checkout_timeout() -> None:
    """The subclass exists because there is no pool event for this. If a SQLAlchemy
    upgrade moves `_do_get`, this test is what says so."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=CountingQueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.05,
    )
    try:
        async with engine.connect():
            with pytest.raises(SATimeoutError):
                async with engine.connect():
                    pass
    finally:
        await engine.dispose()

    assert pool_wait_timeouts_in_window() == 1, (
        "a checkout that timed out was not counted"
    )


# The description rules (CLAUDE.md "Tool descriptions", ADR 0030)


def _description() -> str:
    definition = get_tool("get_postgres_health")
    assert definition is not None
    return definition.description


def test_the_description_says_which_clock_and_that_nothing_is_paged() -> None:
    text = _description().lower()

    assert "clock" in text
    assert "no arguments" in text or "takes no arguments" in text
    assert "offset" in text


def test_the_description_says_whose_pool_the_pool_numbers_describe() -> None:
    """A pool number from an unstated process is the confident-answer failure."""
    text = _description().lower()

    assert "answered" in text, "the description must name the process it read"
    assert "worker" in text, "it must say which pools it cannot see"


def test_the_pool_field_descriptions_repeat_the_scope() -> None:
    """A field read on its own must not lose the qualifier the tool text carries."""
    schema = PostgresHealthOutput.model_json_schema()

    for field in ("pool_checked_out", "pool_overflow", "pool_wait_timeouts_1m"):
        described = schema["properties"][field].get("description", "").lower()
        assert "answered" in described, f"{field} does not say whose pool it is"


def test_the_two_promised_query_fields_say_they_are_unavailable() -> None:
    """They ship as the plan specifies them and are always null here, which the
    description states rather than leaving a reader to infer from one call."""
    schema = PostgresHealthOutput.model_json_schema()

    for field in ("p95_query_ms_1m", "slow_query_count_1m"):
        described = schema["properties"][field].get("description", "").lower()
        assert "null" in described
        assert "query_stats_unknown_reason" in described
