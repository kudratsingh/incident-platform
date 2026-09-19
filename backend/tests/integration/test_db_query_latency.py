"""The A1 world on a real Postgres: the queries run long, the pool does not fill (WO-R3-218).

Both halves of the signature are asserted in one reading, because the family's value is the pair
and either half alone is true of more than one fault: `longest_active_query_ms` and
`active_queries_over_slow_threshold` up, `pool_checked_out` and `pool_wait_timeouts_1m` unmoved.
The reading is taken on a *second* engine, standing in for the MCP process, which is the only way
to prove the cross-process property the fault depends on (ADR 0030, ADR 0034). Plus the property
no unit test can check — that the reading never dips back under the threshold between queries —
and the three teardowns a scenario leans on.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from app.config import Settings
from app.core.db_pool_stats import CountingQueuePool
from app.core.scopes import Scope
from app.core.tenant_scope import platform_session_factory
from app.dependencies import Principal
from app.mcp.registry import ToolContext
from app.mcp.tools import health
from app.mcp.tools.health import (
    SLOW_QUERY_THRESHOLD_MS,
    PostgresHealthOutput,
    get_postgres_health,
)
from app.models.base import Base
from app.workers import db_slow_query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
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

#: The shortest chunk the hook accepts, which keeps this file fast and exercises the tightest
#: case of the continuity guarantee at the same time.
_CHUNK_MS = db_slow_query.MIN_QUERY_MS

#: Capacity 8, so the budget is `SLEEPER_COUNT` and six connections stay free above it.
_WORKER_POOL_SIZE = 4
_WORKER_MAX_OVERFLOW = 4


@pytest.fixture(scope="module")
def pg() -> Any:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


@pytest.fixture(scope="module")
def schema(pg: Any) -> None:
    """Every table once per module: the hook's targets are three real relations.

    Sync, with its own loop: the tier's loops are per-test (`asyncio_mode = "auto"` with no
    module-scoped loop), and `create_all` must run exactly once — a second pass would try to
    create the enum types again.
    """

    async def _create() -> None:
        engine = create_async_engine(pg.get_connection_url())
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()

    asyncio.run(_create())


@pytest_asyncio.fixture
async def worker_engine(
    pg: Any, schema: None
) -> AsyncGenerator[AsyncEngine, None]:
    """The pool the fault lives in — the API and worker process's."""
    built = create_async_engine(
        pg.get_connection_url(),
        pool_size=_WORKER_POOL_SIZE,
        max_overflow=_WORKER_MAX_OVERFLOW,
    )
    try:
        yield built
    finally:
        await built.dispose()


@pytest_asyncio.fixture
async def reader_engine(
    pg: Any, schema: None
) -> AsyncGenerator[AsyncEngine, None]:
    """The pool the *reading* comes from, with the counting pool `app/dependencies.py` builds
    for a real database. A separate engine is a separate pool, which is what makes this the MCP
    process's stand-in rather than a second view of the worker's."""
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


@pytest.fixture
def worker_factory(worker_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Platform-scoped, as the worker's is: the sleeper reads a tenant table (ADR 0026)."""
    return platform_session_factory(worker_engine)


class _Redis:
    """One key, with an expiry this test can drive by the clock."""

    def __init__(self) -> None:
        self._value: str | None = None
        self._expires_at: float | None = None

    async def get(self, _key: str) -> str | None:
        if self._expires_at is not None and time.monotonic() >= self._expires_at:
            self._value = None
            self._expires_at = None
        return self._value

    async def set(self, _key: str, value: Any, ex: int | None = None) -> bool:
        self._value = str(value)
        self._expires_at = time.monotonic() + ex if ex is not None else None
        return True

    async def delete(self, *_keys: str) -> int:
        removed = 1 if self._value is not None else 0
        self._value = None
        self._expires_at = None
        return removed


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
    """`get_postgres_health` exactly as the agent gets it, on the reader's own pool."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            return await get_postgres_health(health._EmptyIn(), _ctx(session))


def _chaos_on() -> Any:
    return patch.object(
        db_slow_query,
        "get_settings",
        return_value=Settings(chaos_enabled=True, environment="test"),
    )


async def _wait_until_slow(engine: AsyncEngine, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = await _read(engine)
        if (out.active_queries_over_slow_threshold or 0) >= 1:
            return True
        await asyncio.sleep(0.1)
    return False


async def _wait_until_normal(engine: AsyncEngine, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = await _read(engine)
        if (out.active_queries_over_slow_threshold or 0) == 0:
            return True
        await asyncio.sleep(0.1)
    return False


@asynccontextmanager
async def _armed(
    factory: async_sessionmaker[AsyncSession],
    redis: _Redis,
    reader: AsyncEngine,
    *,
    target: str = "job_reads",
    ttl_seconds: int | None = None,
) -> AsyncIterator[asyncio.Task[None]]:
    """The fault running, the way the hook arms it: one key, then the worker's task."""
    await redis.set(
        db_slow_query.SLOW_QUERY_KEY,
        db_slow_query.encode_request(target, _CHUNK_MS),
        ex=ttl_seconds,
    )
    with _chaos_on(), patch.object(db_slow_query, "POLL_INTERVAL_SECONDS", 0.05):
        task = asyncio.create_task(db_slow_query.run_slow_queries(factory, redis))
        try:
            yield task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await redis.delete(db_slow_query.SLOW_QUERY_KEY)
            # Not asserted here: a failure in the body must not be masked by one in teardown.
            await _wait_until_normal(reader)


async def test_a_quiet_world_reads_normal_on_both_halves(
    reader_engine: AsyncEngine,
) -> None:
    """The baseline the assertions below are differences from, and a reading that has to be
    sayable: 0 over the threshold is a measurement, not an unknown."""
    out = await _read(reader_engine)

    assert out.ok is True
    assert out.active_queries_over_slow_threshold == 0
    assert out.pool_stats_unknown_reason is None
    assert out.pool_wait_timeouts_1m == 0
    assert out.pool_checked_out == 1, "only this call's own connection"


async def test_both_halves_of_the_signature_are_read_at_once(
    worker_factory: async_sessionmaker[AsyncSession], reader_engine: AsyncEngine
) -> None:
    """The A1 signature, restated for the readings this platform can take (ADR 0030): queries
    slow AND pool fine, in one response, read from a process the fault does not live in."""
    baseline = await _read(reader_engine)
    redis = _Redis()

    async with _armed(worker_factory, redis, reader_engine):
        assert await _wait_until_slow(reader_engine)
        out = await _read(reader_engine)

    # Queries slow.
    assert out.longest_active_query_ms is not None
    assert out.longest_active_query_ms >= SLOW_QUERY_THRESHOLD_MS
    assert out.longest_active_query_ms <= _CHUNK_MS + 2000, (
        "a query outlived one chunk, so the residue bound does not hold"
    )
    assert (out.active_queries_over_slow_threshold or 0) >= 1

    # Pool fine — on the pool that answered, which is not the pool the fault is in.
    assert out.pool_stats_unknown_reason is None
    assert out.pool_wait_timeouts_1m == 0, "nothing waited for a connection"
    assert out.pool_checked_out == baseline.pool_checked_out
    assert out.pool_overflow == 0

    # And the field the plan asked for is still null, with its reason.
    assert out.p95_query_ms_1m is None
    assert out.slow_query_count_1m is None
    assert out.query_stats_unknown_reason is not None


async def test_the_reading_never_dips_back_under_the_threshold(
    worker_factory: async_sessionmaker[AsyncSession], reader_engine: AsyncEngine
) -> None:
    """The property the whole fixture rests on, and the one only a real server can show: with
    one sleeper the over-threshold count reads 0 for the first 500 ms of every chunk, so an
    agent reading at the wrong moment sees a healthy database in an unhealthy world. Sampled
    across more than two chunks at the tightest chunk the hook accepts."""
    redis = _Redis()
    samples: list[int] = []

    async with _armed(worker_factory, redis, reader_engine):
        assert await _wait_until_slow(reader_engine)
        deadline = time.monotonic() + (_CHUNK_MS / 1000) * 2.5
        while time.monotonic() < deadline:
            out = await _read(reader_engine)
            samples.append(out.active_queries_over_slow_threshold or 0)
            await asyncio.sleep(0.1)

    assert len(samples) >= 20, f"too few samples to mean anything: {len(samples)}"
    assert all(count >= 1 for count in samples), (
        f"the reading dipped under the threshold between queries: {samples}"
    )


async def test_the_slow_query_really_reads_the_declared_target(
    worker_factory: async_sessionmaker[AsyncSession], reader_engine: AsyncEngine
) -> None:
    """`target` is a scope, not decoration: the statement names the relation it declares, so
    `pg_stat_activity` says which read path is slow. No tool returns query text — this test
    reads the server directly."""
    redis = _Redis()

    async with _armed(worker_factory, redis, reader_engine, target="outbox_reads"):
        assert await _wait_until_slow(reader_engine)
        factory = async_sessionmaker(reader_engine, expire_on_commit=False)
        async with factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT query FROM pg_stat_activity WHERE state = 'active' "
                        "AND datname = current_database() AND pid <> pg_backend_pid()"
                    )
                )
            ).scalars()
            running = list(rows)

    assert any("outbox_events" in q for q in running), running
    assert any("pg_sleep" in q for q in running), running


async def test_clearing_the_flag_restores_normal_timing_with_no_manual_step(
    worker_factory: async_sessionmaker[AsyncSession], reader_engine: AsyncEngine
) -> None:
    """What `make eval-reset`'s `chaos:*` sweep does. The bound is one chunk: the query already
    running finishes, and no further one starts."""
    redis = _Redis()

    async with _armed(worker_factory, redis, reader_engine) as task:
        assert await _wait_until_slow(reader_engine)
        await redis.delete(db_slow_query.SLOW_QUERY_KEY)
        started = time.monotonic()
        assert await _wait_until_normal(reader_engine)
        assert time.monotonic() - started <= (_CHUNK_MS / 1000) + 3
        assert not task.done()


async def test_expiry_restores_normal_timing_with_no_manual_step(
    worker_factory: async_sessionmaker[AsyncSession], reader_engine: AsyncEngine
) -> None:
    """The TTL is the teardown a scenario leans on: nothing has to be called."""
    redis = _Redis()

    async with _armed(worker_factory, redis, reader_engine, ttl_seconds=3):
        assert await _wait_until_slow(reader_engine)
        assert await _wait_until_normal(reader_engine, timeout=20)


async def test_a_restart_leaves_at_most_one_chunk_behind(
    worker_factory: async_sessionmaker[AsyncSession], reader_engine: AsyncEngine
) -> None:
    """The third teardown. Cancelling the task is what a worker restart does to it, and the
    driver's own cancel usually makes this faster than the bound."""
    redis = _Redis()
    await redis.set(
        db_slow_query.SLOW_QUERY_KEY,
        db_slow_query.encode_request("job_reads", _CHUNK_MS),
    )
    with _chaos_on(), patch.object(db_slow_query, "POLL_INTERVAL_SECONDS", 0.05):
        task = asyncio.create_task(db_slow_query.run_slow_queries(worker_factory, redis))
        assert await _wait_until_slow(reader_engine)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    started = time.monotonic()
    assert await _wait_until_normal(reader_engine)
    assert time.monotonic() - started <= (_CHUNK_MS / 1000) + 3
    await redis.delete(db_slow_query.SLOW_QUERY_KEY)


async def test_the_worker_pool_keeps_its_free_floor(
    worker_engine: AsyncEngine,
    worker_factory: async_sessionmaker[AsyncSession],
    reader_engine: AsyncEngine,
) -> None:
    """A fault, not an outage: the sleepers take `SLEEPER_COUNT` connections and the loops can
    still get one straight away."""
    redis = _Redis()

    async with _armed(worker_factory, redis, reader_engine):
        assert await _wait_until_slow(reader_engine)
        held = worker_engine.sync_engine.pool.checkedout()
        assert held <= db_slow_query.SLEEPER_COUNT

        async with asyncio.timeout(2):
            async with worker_engine.connect() as conn:
                assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1


async def test_a_flag_naming_no_declared_target_runs_nothing(
    worker_factory: async_sessionmaker[AsyncSession], reader_engine: AsyncEngine
) -> None:
    """Refused rather than matched against nothing, on the task's side of the key too: a value
    the hook could not have written leaves the world normal instead of guessing at a relation."""
    redis = _Redis()
    await redis.set(db_slow_query.SLOW_QUERY_KEY, "users:2000")
    with _chaos_on(), patch.object(db_slow_query, "POLL_INTERVAL_SECONDS", 0.05):
        task = asyncio.create_task(db_slow_query.run_slow_queries(worker_factory, redis))
        try:
            await asyncio.sleep(1.5)
            out = await _read(reader_engine)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await redis.delete(db_slow_query.SLOW_QUERY_KEY)

    assert out.active_queries_over_slow_threshold == 0
