"""The A2 world on a real Postgres: the pool starves, the queries do not (WO-R3-219, WP-8.3).

All three facts of the signature are asserted together, because a test that asserted only
saturation would pass for A1 as well: connections checked out at the pool's ceiling, an
acquisition that waits and then times out once the free floor is gone, and a normal query time on
a connection that did get one. Plus the two teardowns a scenario depends on — the key going away
(what `make eval-reset` does) and the key expiring — and the floor that keeps the loops working.
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
from app.config import Settings
from app.core.tenant_scope import platform_session_factory
from app.models.base import Base
from app.models.outbox import OutboxEvent
from app.models.tenant import Tenant
from app.workers import db_pool_hold
from sqlalchemy import select, text
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

try:
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    _HAS_TC = True
except Exception:  # pragma: no cover
    _HAS_TC = False


def _has_docker() -> bool:
    try:
        subprocess.run(
            ["docker", "info"], capture_output=True, timeout=30, check=True
        )
        return True
    except Exception:  # pragma: no cover - environment-dependent
        return False


pytestmark = pytest.mark.skipif(
    not _HAS_TC or not _has_docker(),
    reason="needs Docker + testcontainers[postgres]",
)

#: A deliberately small pool, so the whole signature fits in eight connections: capacity 8 means
#: the clamp holds 4 and leaves exactly `MIN_FREE_CONNECTIONS` acquirable.
_POOL_SIZE = 4
_MAX_OVERFLOW = 4
_CAPACITY = _POOL_SIZE + _MAX_OVERFLOW

#: Short, so "waits then gives up" is a second rather than the production 30.
_POOL_TIMEOUT = 1.0

_TABLES = [Tenant.__table__, OutboxEvent.__table__]


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


@pytest.fixture(scope="module")
def pg() -> Any:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


@pytest.fixture
async def factory(pg: Any) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """The factory the worker is handed: platform-scoped, on a pool small enough to fill."""
    engine = create_async_engine(
        pg.get_connection_url(),
        pool_size=_POOL_SIZE,
        max_overflow=_MAX_OVERFLOW,
        pool_timeout=_POOL_TIMEOUT,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    yield platform_session_factory(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all, tables=list(reversed(_TABLES)))
    await engine.dispose()


def _checked_out(factory: async_sessionmaker[AsyncSession]) -> int:
    engine = factory.kw["bind"]
    return int(engine.sync_engine.pool.checkedout())


def _chaos_on() -> Any:
    return patch.object(
        db_pool_hold,
        "get_settings",
        return_value=Settings(chaos_enabled=True, environment="test"),
    )


async def _wait_for_checkout(
    factory: async_sessionmaker[AsyncSession], expected: int, timeout: float = 15.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _checked_out(factory) == expected:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"pool still shows {_checked_out(factory)} checked out, expected {expected}"
    )


# The pool really is what gets held


async def test_the_capacity_is_read_off_the_real_pool(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    assert db_pool_hold.pool_capacity(factory) == _CAPACITY
    # …and the clamp on this pool is the floor, not the static ceiling.
    assert db_pool_hold.clamp_to_pool(
        db_pool_hold.MAX_HELD_CONNECTIONS, _CAPACITY
    ) == _CAPACITY - db_pool_hold.MIN_FREE_CONNECTIONS


async def test_a_hold_checks_connections_out_and_gives_them_back(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    held: list[AsyncSession] = []
    wanted = db_pool_hold.clamp_to_pool(db_pool_hold.MAX_HELD_CONNECTIONS, _CAPACITY)
    try:
        assert await db_pool_hold.reconcile_held(held, wanted, factory) == wanted
        assert _checked_out(factory) == wanted
    finally:
        await db_pool_hold._release(held)
    assert _checked_out(factory) == 0


async def test_a_held_connection_declares_platform_scope(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """ADR 0026: an unscoped statement is refused under the strict policies, so the holder's own
    statements have to declare themselves. The factory's `after_begin` hook is what does it, and
    this asserts it fired on the transaction the hold leaves open."""
    held: list[AsyncSession] = []
    try:
        await db_pool_hold.reconcile_held(held, 1, factory)
        scope = (
            await held[0].execute(text("SELECT current_setting('app.tenant_scope', true)"))
        ).scalar_one()
        assert scope == "platform"
    finally:
        await db_pool_hold._release(held)


# All three facts of the A2 signature, in one window


async def test_the_signature_is_saturation_plus_waits_plus_normal_queries(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The discrimination the family turns on. A test that asserted saturation alone would pass
    for a slow-query world too."""
    held: list[AsyncSession] = []
    floor: list[AsyncSession] = []
    try:
        wanted = db_pool_hold.clamp_to_pool(
            db_pool_hold.MAX_HELD_CONNECTIONS, _CAPACITY
        )
        await db_pool_hold.reconcile_held(held, wanted, factory)

        # (1) checked out at the pool's own ceiling, with the overflow reachable.
        assert _checked_out(factory) == wanted >= _POOL_SIZE

        # (3) a connection that *is* acquired runs its query at normal speed — this is what
        # separates A2 from A1, so it is asserted inside the same window.
        started = time.monotonic()
        async with factory() as session:
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
        assert time.monotonic() - started < _POOL_TIMEOUT

        # The floor is real: `MIN_FREE_CONNECTIONS` more callers get connections.
        await db_pool_hold.reconcile_held(
            floor, db_pool_hold.MIN_FREE_CONNECTIONS, factory
        )
        assert _checked_out(factory) == _CAPACITY

        # (2) with the floor spent, the next acquisition waits and then gives up — a wait
        # timeout, which is the counter the read tool reports as rising.
        started = time.monotonic()
        with pytest.raises(SATimeoutError):
            async with factory() as session:
                await session.execute(text("SELECT 1"))
        assert time.monotonic() - started >= _POOL_TIMEOUT
    finally:
        await db_pool_hold._release(floor)
        await db_pool_hold._release(held)


async def test_a_loops_unit_of_work_still_completes_while_the_pool_is_held(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The order's "no worker loop is starved" requirement, at the shape of the work rather than
    the loop: an outbox write and read-back, which is the relay's pass, completes inside the
    window because the clamp left the floor free."""
    held: list[AsyncSession] = []
    try:
        await db_pool_hold.reconcile_held(
            held,
            db_pool_hold.clamp_to_pool(db_pool_hold.MAX_HELD_CONNECTIONS, _CAPACITY),
            factory,
        )
        tenant_id = uuid.uuid4()
        async with factory() as session:
            async with session.begin():
                session.add(
                    Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[:8]}", name="held")
                )
                session.add(
                    OutboxEvent(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        topic="job.submitted",
                        key=f"{tenant_id}:{uuid.uuid4()}",
                        payload={"event": "job.submitted"},
                    )
                )
        async with factory() as session:
            waiting = (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.tenant_id == tenant_id)
                )
            ).scalars().all()
        assert len(waiting) == 1
    finally:
        await db_pool_hold._release(held)


# Teardown: the key going away, and the key expiring


async def test_deleting_the_key_releases_every_connection(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """What `make eval-reset` does: it sweeps `chaos:*`, and the holder gives the connections
    back on its next pass even though the TTL has not expired."""
    redis = _Redis()
    await redis.set(db_pool_hold.hold_key(), db_pool_hold.MAX_HELD_CONNECTIONS, ex=3600)
    with _chaos_on(), patch.object(db_pool_hold, "POLL_INTERVAL_SECONDS", 0.05):
        task = asyncio.create_task(db_pool_hold.hold_db_pool(factory, redis))
        try:
            await _wait_for_checkout(
                factory, _CAPACITY - db_pool_hold.MIN_FREE_CONNECTIONS
            )
            await redis.delete(db_pool_hold.hold_key())
            await _wait_for_checkout(factory, 0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert _checked_out(factory) == 0


async def test_an_expired_key_releases_every_connection(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The TTL is the teardown that needs nobody: a scenario that crashes mid-run still heals."""
    redis = _Redis()
    await redis.set(db_pool_hold.hold_key(), 2, ex=2)
    with _chaos_on(), patch.object(db_pool_hold, "POLL_INTERVAL_SECONDS", 0.05):
        task = asyncio.create_task(db_pool_hold.hold_db_pool(factory, redis))
        try:
            await _wait_for_checkout(factory, 2)
            await _wait_for_checkout(factory, 0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert _checked_out(factory) == 0
