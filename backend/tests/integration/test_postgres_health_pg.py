"""`get_postgres_health`'s Postgres-only readings, on a real Postgres.

The unit tier runs SQLite, so every field that comes from `pg_stat_activity` or a queue pool
is exercised only in its degraded form there. This is the other half: the pool numbers are
real, a query that is genuinely slow moves `longest_active_query_ms` and the over-threshold
count — which is what makes those two a signal a lab can move, where `p95_query_ms_1m` is
not — and the per-minute query fields degrade with the *extension-absent* reason rather than
the not-Postgres one (ADR 0030, divergence H6).
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from app.core.db_pool_stats import CountingQueuePool
from app.core.scopes import Scope
from app.dependencies import Principal
from app.mcp.registry import ToolContext
from app.mcp.tools import health
from app.mcp.tools.health import (
    QUERY_STATS_UNKNOWN_EXTENSION_ABSENT,
    SLOW_QUERY_THRESHOLD_MS,
    PostgresHealthOutput,
    get_postgres_health,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

try:
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    _HAS_TC = True
except Exception:  # pragma: no cover
    _HAS_TC = False


def _has_docker() -> bool:
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=True)
        return True
    except Exception:  # pragma: no cover - environment-dependent
        return False


pytestmark = pytest.mark.skipif(
    not _HAS_TC or not _has_docker(),
    reason="needs Docker + testcontainers[postgres]",
)

#: Long enough that a reading taken while it runs is unambiguously past the threshold.
_SLOW_QUERY_SECONDS = 3.0


@pytest.fixture(scope="module")
def pg() -> Any:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


@pytest_asyncio.fixture
async def engine(pg: Any) -> AsyncGenerator[AsyncEngine, None]:
    """The counting pool, as `app/dependencies.py` builds it for a real database."""
    built = create_async_engine(
        pg.get_connection_url(),
        poolclass=CountingQueuePool,
        pool_size=5,
        max_overflow=3,
    )
    try:
        yield built
    finally:
        await built.dispose()


def _ctx(db: Any) -> ToolContext:
    return ToolContext(
        db=db,
        redis=object(),  # type: ignore[arg-type]  — this tool never touches Redis
        principal=Principal(
            kind="service_account",
            tenant_id=uuid.uuid4(),
            scopes=frozenset({Scope.TELEMETRY_READ.value}),
        ),
    )


async def _read(engine: AsyncEngine) -> PostgresHealthOutput:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            return await get_postgres_health(health._EmptyIn(), _ctx(session))


async def test_the_pool_numbers_are_real_on_a_queue_pool(engine: AsyncEngine) -> None:
    """Non-null, and interpretable: a checked-out count with no ceiling beside it cannot
    be read."""
    out = await _read(engine)

    assert out.ok is True
    assert out.dialect == "postgresql"
    assert out.pool_stats_unknown_reason is None
    assert out.pool_size == 5
    assert out.pool_max_overflow == 3
    assert out.pool_checked_out is not None and out.pool_checked_out >= 1
    assert out.pool_overflow is not None
    assert out.pool_wait_timeouts_1m == 0, "nothing waited, which is a measurement"
    assert out.active_connections is not None and out.active_connections >= 1


async def test_a_quiet_database_reports_no_slow_queries(engine: AsyncEngine) -> None:
    """The healthy reading has to be sayable: 0 over the threshold, not null."""
    out = await _read(engine)

    assert out.active_queries_over_slow_threshold == 0
    assert out.slow_query_threshold_ms == SLOW_QUERY_THRESHOLD_MS
    # `longest_active_query_ms` may be null (nothing else running) or a small number.
    if out.longest_active_query_ms is not None:
        assert out.longest_active_query_ms < SLOW_QUERY_THRESHOLD_MS


async def test_a_genuinely_slow_query_moves_both_live_readings(
    engine: AsyncEngine,
) -> None:
    """The signal Family A needs, and the one a lab can actually move: a query held open
    on another connection is visible to this reading, because it comes from the server."""
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _hold() -> None:
        async with factory() as session:
            await session.execute(text(f"SELECT pg_sleep({_SLOW_QUERY_SECONDS})"))

    holder = asyncio.create_task(_hold())
    try:
        await asyncio.sleep(1.0)  # past the 500 ms threshold, well short of the sleep
        out = await _read(engine)
    finally:
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)

    assert out.longest_active_query_ms is not None
    assert out.longest_active_query_ms >= SLOW_QUERY_THRESHOLD_MS, (
        "a query running for a second did not show up as the longest active one"
    )
    assert out.active_queries_over_slow_threshold is not None
    assert out.active_queries_over_slow_threshold >= 1


async def test_the_per_minute_query_fields_degrade_with_a_stable_reason(
    engine: AsyncEngine,
) -> None:
    """H6, asserted rather than assumed: this Postgres has no `pg_stat_statements`, so the
    two promised fields are null and the reason says *that* rather than "not Postgres"."""
    out = await _read(engine)

    assert out.p95_query_ms_1m is None
    assert out.slow_query_count_1m is None
    assert out.query_stats_unknown_reason == QUERY_STATS_UNKNOWN_EXTENSION_ABSENT


async def test_the_reading_survives_a_failing_statement_in_the_same_session(
    engine: AsyncEngine,
) -> None:
    """R2-59 on the dialect that actually aborts: a degraded probe must leave the session
    writable, and the pool numbers are in-process so they stay real."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            first = await get_postgres_health(health._EmptyIn(), _ctx(session))
            assert first.ok is True

            # A write after the probe still lands — the probe's savepoint did not poison
            # the transaction.
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
