"""The D1 proof: the pool `saturate_db_pool` holds is readable from the other process.

WO-R3-219 shipped a hook that holds the worker's connections and WO-R3-217 shipped a reading
that describes the answering process's own pool, so the fault was real and the agent's surface
stayed healthy (ADR 0030 § what this does not close). This is the only test that proves
otherwise, and it needs both real things to prove it: a real Postgres, so the pool is a pool
that can actually be held, and a real Redis, so the writer and the reader are genuinely two
clients sharing nothing.

The shape of it: two engines with two pools, the way the worker and the MCP process have two.
The hook is armed through the tool, the worker's holder takes the connections, the worker's
gauge publishes on its cadence, and the reading is taken on the MCP side — where the flat
`pool_*` fields still describe the MCP pool, and the `pools` group carries the worker's held
count beside them. Red before ADR 0033: `get_postgres_health` had no `pools` field at all.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from app.config import Settings
from app.core.pool_state import (
    POOL_GAUGES_UNKNOWN_NONE_PUBLISHED,
    PROCESS_API_WORKER,
    PROCESS_MCP,
    pool_key_for,
    publish_pool_state,
    start_pool_gauge,
    stop_pool_gauge,
)
from app.core.scopes import Scope
from app.core.tenant_scope import platform_session_factory
from app.dependencies import Principal
from app.mcp.registry import ToolContext, get_tool
from app.mcp.tools.chaos.saturate_db_pool import (
    SaturateDbPoolInput,
    saturate_db_pool,
)
from app.mcp.tools.health import PostgresHealthOutput, get_postgres_health
from app.workers import db_pool_hold
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

try:
    import docker  # noqa: F401
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    _HAS_TC = True
except Exception:  # pragma: no cover - testcontainers or docker not installed
    _HAS_TC = False


def _docker_running() -> bool:
    if not _HAS_TC:
        return False
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=True)
        return True
    except Exception:  # pragma: no cover - environment-dependent
        return False


pytestmark = pytest.mark.skipif(
    not _docker_running(), reason="needs Docker + testcontainers[postgres]"
)

#: Small enough that the whole signature fits in eight connections: the clamp holds four and
#: leaves exactly `MIN_FREE_CONNECTIONS` acquirable.
_POOL_SIZE = 4
_MAX_OVERFLOW = 4
_CAPACITY = _POOL_SIZE + _MAX_OVERFLOW

#: The gauge's cadence, compressed so the test does not wait ten seconds for a pass.
_GAUGE_INTERVAL = 0.1


def _find_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def pg() -> Any:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


@pytest_asyncio.fixture(scope="module")
async def redis_url() -> AsyncIterator[str]:
    """A real Redis, so the writer and the reader below are genuinely two clients."""
    host_port = _find_free_port()
    container = (
        DockerContainer("redis:7-alpine")
        .with_command(f"redis-server --port {host_port}")
        .with_bind_ports(host_port, host_port)
    )
    container.start()
    try:
        wait_for_logs(container, "Ready to accept connections", timeout=60)
        yield f"redis://localhost:{host_port}/0"
    finally:
        container.stop()


@pytest_asyncio.fixture
async def writer(redis_url: str) -> AsyncIterator[Redis]:
    """The worker process's client."""
    client: Redis = Redis.from_url(redis_url, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def reader(redis_url: str) -> AsyncIterator[Redis]:
    """The read surface's client: its own pool, no shared Python state."""
    client: Redis = Redis.from_url(redis_url, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def worker_factory(pg: Any) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """The factory the worker is handed: platform-scoped, on a pool small enough to fill."""
    engine = create_async_engine(
        pg.get_connection_url(),
        pool_size=_POOL_SIZE,
        max_overflow=_MAX_OVERFLOW,
        pool_timeout=1.0,
    )
    yield platform_session_factory(engine)
    await engine.dispose()


@pytest_asyncio.fixture
async def mcp_session(pg: Any) -> AsyncIterator[AsyncSession]:
    """The read surface's own engine — a second pool, as the second process has."""
    engine = create_async_engine(pg.get_connection_url(), pool_size=2, max_overflow=2)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _worker_pool(factory: async_sessionmaker[AsyncSession]) -> Any:
    return factory.kw["bind"].sync_engine.pool


def _checked_out(factory: async_sessionmaker[AsyncSession]) -> int:
    return int(_worker_pool(factory).checkedout())


def _chaos_on() -> Any:
    return patch.object(
        db_pool_hold,
        "get_settings",
        return_value=Settings(chaos_enabled=True, environment="test"),
    )


def _ctx(db: AsyncSession, redis: Redis) -> ToolContext:
    return ToolContext(
        db=db,
        redis=redis,
        principal=Principal(
            kind="service_account",
            tenant_id=uuid.uuid4(),
            scopes=frozenset({Scope.TELEMETRY_READ.value}),
        ),
    )


def _chaos_ctx(redis: Redis) -> ToolContext:
    return ToolContext(
        db=object(),  # type: ignore[arg-type]  — this hook only writes a flag
        redis=redis,
        principal=Principal(
            kind="service_account",
            tenant_id=uuid.uuid4(),
            scopes=frozenset({Scope.CHAOS_INVOKE.value}),
        ),
    )


async def _wait_for_checkout(
    factory: async_sessionmaker[AsyncSession], expected: int, timeout: float = 20.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _checked_out(factory) == expected:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"pool still shows {_checked_out(factory)} checked out, expected {expected}"
    )


async def _wait_for_gauge(reader: Redis, process: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await reader.get(pool_key_for(process)) is not None:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"no gauge published for {process}")


async def _health(db: AsyncSession, redis: Redis) -> PostgresHealthOutput:
    """Built from the registered input model, so the call goes through the contract."""
    spec = get_tool("get_postgres_health")
    assert spec is not None
    return await get_postgres_health(spec.input_model(), _ctx(db, redis))


async def test_the_held_worker_pool_is_readable_from_the_other_process(
    worker_factory: async_sessionmaker[AsyncSession],
    mcp_session: AsyncSession,
    writer: Redis,
    reader: Redis,
) -> None:
    """The order, end to end: arm the hook, let the worker hold the connections, read the
    worker's gauge from the MCP side — where the flat fields still say the MCP pool."""
    armed = await saturate_db_pool(
        SaturateDbPoolInput(connections=db_pool_hold.MAX_HELD_CONNECTIONS, ttl_seconds=300),
        _chaos_ctx(writer),
    )
    assert armed.accepted

    expected_held = db_pool_hold.clamp_to_pool(
        db_pool_hold.MAX_HELD_CONNECTIONS, _CAPACITY
    )
    with _chaos_on(), patch.object(db_pool_hold, "POLL_INTERVAL_SECONDS", 0.05):
        holder = asyncio.create_task(db_pool_hold.hold_db_pool(worker_factory, writer))
        await start_pool_gauge(
            process=PROCESS_API_WORKER,
            pool_getter=lambda: _worker_pool(worker_factory),
            redis=writer,
            interval=_GAUGE_INTERVAL,
        )
        try:
            await _wait_for_checkout(worker_factory, expected_held)
            await _wait_for_gauge(reader, PROCESS_API_WORKER)
            # One more pass, so the published number is from after the hold landed.
            await asyncio.sleep(_GAUGE_INTERVAL * 3)

            out = await _health(mcp_session, reader)
        finally:
            await stop_pool_gauge()
            holder.cancel()
            await asyncio.gather(holder, return_exceptions=True)

    assert out.ok
    assert out.pool_gauges_unknown_reason is None

    gauge = next(p for p in out.pools if p.process == PROCESS_API_WORKER)
    assert gauge.checked_out >= expected_held
    assert gauge.size == _POOL_SIZE
    assert gauge.max_overflow == _MAX_OVERFLOW
    assert gauge.reported_age_s >= 0.0

    # The contrast that made this order necessary: the answering process's own pool is
    # untouched, and its flat fields say so.
    assert out.pool_checked_out is not None
    assert out.pool_checked_out < expected_held
    assert out.pool_stats_unknown_reason is None


async def test_nobody_publishing_reads_as_unknown_not_as_healthy_pools(
    mcp_session: AsyncSession, reader: Redis, writer: Redis
) -> None:
    """On a real Redis with a real empty namespace, not just against a stub."""
    out = await _health(mcp_session, reader)

    assert out.pools == ()
    assert out.pool_gauges_unknown_reason == POOL_GAUGES_UNKNOWN_NONE_PUBLISHED
    # …while the answering process's own pool is still reported, because that reading
    # never needed the store.
    assert out.pool_checked_out is not None


async def test_the_gauge_is_not_swept_by_the_lab_namespace(writer: Redis) -> None:
    """`reset_eval_state.py` scans `chaos:*`. This is a platform key, so a world reset
    leaves it — stated on a real Redis rather than only asserted on the constant."""
    await publish_pool_state(
        writer,
        process=PROCESS_API_WORKER,
        size=_POOL_SIZE,
        checked_out=_CAPACITY,
        overflow=_MAX_OVERFLOW,
        max_overflow=_MAX_OVERFLOW,
        wait_timeouts_1m=4,
    )

    chaos_keys = [k async for k in writer.scan_iter(match="chaos:*")]

    assert pool_key_for(PROCESS_API_WORKER) not in chaos_keys
    assert await writer.get(pool_key_for(PROCESS_API_WORKER)) is not None


async def test_a_process_that_stops_publishing_drops_out(
    mcp_session: AsyncSession, writer: Redis, reader: Redis
) -> None:
    """The TTL doing its job on Redis's own clock: a wedged process stops being a reading
    rather than freezing at its last healthy number."""
    with patch("app.core.pool_state.POOL_STATE_TTL_SECONDS", 1):
        await publish_pool_state(
            writer,
            process=PROCESS_MCP,
            size=_POOL_SIZE,
            checked_out=1,
            overflow=0,
            max_overflow=_MAX_OVERFLOW,
            wait_timeouts_1m=0,
        )

    assert (await _health(mcp_session, reader)).pools != ()

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if await reader.get(pool_key_for(PROCESS_MCP)) is None:
            break
        await asyncio.sleep(0.1)

    out = await _health(mcp_session, reader)
    assert out.pools == ()
    assert out.pool_gauges_unknown_reason == POOL_GAUGES_UNKNOWN_NONE_PUBLISHED
