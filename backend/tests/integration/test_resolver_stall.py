"""Family C's world on a real Postgres: a child stranded `WAITING`, and nothing to fix.

An absence is the discriminator (01 §7.2), so escalating is the right answer. Both
promoters must stop: `kill_consumer('dependency-resolver')` (a group, not a
`ControlLoopName` — H2) and `pause_control_loop('resume_unblocked_waiting')` (H3, ADR 0027).
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from app.config import Settings, get_settings
from app.core.scopes import Scope
from app.dependencies import Principal
from app.mcp.registry import ToolContext
from app.mcp.tools.dag_state import GetDagStateInput, get_dag_state
from app.mcp.tools.list_dlq_messages import (
    ListDlqMessagesInput,
    list_dlq_messages,
)
from app.models.base import Base
from app.models.enums import JobStatus, JobType, UserRole
from app.models.job import Job
from app.models.job_dependency import JobDependency
from app.models.outbox import OutboxEvent
from app.models.saga import Saga
from app.models.tenant import Tenant
from app.models.triage import JobTriage
from app.models.user import User
from app.workers import dispatcher
from app.workers.control_loop_pause import (
    ControlLoopName,
    loop_is_paused,
    pause_key_for,
)
from app.workers.kafka_consumer import kill_key_for
from sqlalchemy import func, select
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

#: The two keys the composed stall is made of, from the shipped helpers so a
#: rename cannot leave this file testing a key nothing reads.
_SWEEP_PAUSE_KEY = pause_key_for(ControlLoopName.RESUME_UNBLOCKED_WAITING)
_RESOLVER_KILL_KEY = kill_key_for(get_settings().kafka_consumer_group_dependency)

#: The window in sweep iterations, not wall seconds: the interval is patched down and
#: the loop runs a fixed count, so a loaded machine cannot shorten it.
_FAST_INTERVAL = 0.01
_MIN_TICKS = 12
_TICK_TIMEOUT = 20.0

#: How old the child looks — 01 §7.2's "created_at age large" fact.
_CHILD_AGE = timedelta(minutes=47)

_TABLES = [
    Tenant.__table__,
    User.__table__,
    Saga.__table__,
    Job.__table__,
    JobDependency.__table__,
    OutboxEvent.__table__,
    JobTriage.__table__,
]


class _World:
    """The database side of one test's world, plus the rows it can seed."""

    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        self.factory = factory
        self.tenant_id = tenant_id
        self.user_id = user_id

    def _job(self, *, status: str, created_at: datetime) -> Job:
        return Job(
            id=uuid.uuid4(),
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            type=JobType.CSV_UPLOAD,
            status=status,
            payload={"rows": 1},
            max_attempts=3,
            created_at=created_at,
            updated_at=created_at,
        )

    async def seed_stranded_chain(self) -> tuple[uuid.UUID, uuid.UUID]:
        """Parent `COMPLETED`, child `WAITING`, one dependency edge — written at the
        data level, because the parent's `job.completed` has already happened."""
        created = datetime.now(UTC) - _CHILD_AGE
        async with self.factory() as session:
            async with session.begin():
                parent = self._job(status=JobStatus.COMPLETED, created_at=created)
                child = self._job(status=JobStatus.WAITING, created_at=created)
                session.add_all([parent, child])
                session.add(
                    JobDependency(job_id=child.id, depends_on_job_id=parent.id)
                )
        return parent.id, child.id

    async def status_of(self, job_id: uuid.UUID) -> str:
        async with self.factory() as session:
            return (
                await session.execute(select(Job.status).where(Job.id == job_id))
            ).scalar_one()

    async def submitted_events(self) -> int:
        """`job.submitted` outbox rows: a status change without one is what the CAS
        in `promote_waiting_to_pending` leaves a loser."""
        async with self.factory() as session:
            return (
                await session.execute(
                    select(func.count())
                    .select_from(OutboxEvent)
                    .where(OutboxEvent.topic == "job.submitted")
                )
            ).scalar_one()

    def ctx(self, session: AsyncSession, redis: Any) -> ToolContext:
        """The agent's own context: a read-scoped machine principal, one tenant."""
        return ToolContext(
            db=session,
            redis=redis,
            principal=Principal(
                kind="service_account",
                tenant_id=self.tenant_id,
                scopes=frozenset(
                    {Scope.TELEMETRY_READ.value, Scope.INCIDENTS_READ.value}
                ),
            ),
        )


class _Redis:
    """`GET` for `loop_is_paused`, `MGET` over `dag:paused:<ancestor>`, `TTL`."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self._ttls: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self._store.get(k) for k in keys]

    async def ttl(self, key: str) -> int:
        if key not in self._store:
            return -2  # redis-py: missing
        return self._ttls.get(key, -1)  # -1: present, no expiry

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self._store[key] = str(value)
        if ex is not None:
            self._ttls[key] = int(ex)
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self._store.pop(key, None) is not None:
                self._ttls.pop(key, None)
                removed += 1
        return removed


@pytest.fixture(scope="module")
def pg() -> Any:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


@pytest.fixture
async def world(pg: Any) -> AsyncIterator[_World]:
    """A fresh schema, one tenant, one user — per test, because a leftover
    `PENDING` child would void the next test's assertion."""
    engine = create_async_engine(pg.get_connection_url(), pool_size=5, max_overflow=5)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            session.add(
                Tenant(
                    id=tenant_id,
                    slug=f"t-{tenant_id.hex[:8]}",
                    name="resolver stall",
                )
            )
            session.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    email=f"owner-{user_id.hex[:8]}@example.com",
                    hashed_password="not-a-real-hash",
                    role=UserRole.USER,
                    is_active=True,
                )
            )

    try:
        yield _World(factory=factory, tenant_id=tenant_id, user_id=user_id)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(
                Base.metadata.drop_all, tables=list(reversed(_TABLES))
            )
        await engine.dispose()


async def _armed(*, pause_sweep: bool) -> _Redis:
    """The lab's two keys, as a scenario's seeding leaves them. The kill key
    (`chaos:kill:<group>`) is set in every case, so the windows differ in one way."""
    redis = _Redis()
    await redis.set(_RESOLVER_KILL_KEY, "killed", ex=300)
    if pause_sweep:
        await redis.set(_SWEEP_PAUSE_KEY, "paused", ex=300)
    return redis


async def _run_sweep_window(
    factory: async_sessionmaker[AsyncSession], redis: _Redis
) -> int:
    """Run the real sweep loop for exactly `_MIN_TICKS` iterations. Patched:
    `_RESUME_SWEEP_INTERVAL`, `CHAOS_ENABLED` (gate 1), and `loop_is_paused` in a
    counter that calls through; `CancelledError` stops it at the top of an iteration.
    """
    ticks = 0

    async def _counting_is_paused(loop_name: ControlLoopName) -> bool:
        nonlocal ticks
        ticks += 1
        if ticks > _MIN_TICKS:
            raise asyncio.CancelledError
        return await loop_is_paused(loop_name)

    with patch(
        "app.workers.control_loop_pause.get_settings",
        return_value=Settings(chaos_enabled=True, environment="test"),
    ), patch("app.core.redis.get_redis_client", return_value=redis), patch.object(
        dispatcher, "_RESUME_SWEEP_INTERVAL", _FAST_INTERVAL
    ), patch.object(dispatcher, "loop_is_paused", _counting_is_paused):
        await asyncio.wait_for(
            dispatcher._resume_unblocked_waiting_loop(factory, redis),
            timeout=_TICK_TIMEOUT,
        )

    return ticks - 1  # the iteration that stopped the loop did no work


async def test_the_composed_stall_holds_the_child_and_reads_as_an_absence(
    world: _World,
) -> None:
    """Both promoters stopped: the child does not move across twelve sweep
    iterations, and every fact 01 §7.2 names comes back from the real read tools."""
    parent_id, child_id = await world.seed_stranded_chain()
    redis = await _armed(pause_sweep=True)

    ticks = await _run_sweep_window(world.factory, redis)

    assert ticks >= _MIN_TICKS
    assert await world.status_of(child_id) == JobStatus.WAITING, (
        "the paused sweep promoted the child anyway"
    )
    assert await world.submitted_events() == 0, (
        "the paused sweep announced a promotion it did not make"
    )

    async with world.factory() as session:
        dag = await get_dag_state(
            GetDagStateInput(job_id=child_id), world.ctx(session, redis)
        )
        dlq = await list_dlq_messages(
            ListDlqMessagesInput(), world.ctx(session, redis)
        )

    statuses = {node.id: node.status for node in dag.nodes}
    assert statuses[str(child_id)] == JobStatus.WAITING
    assert statuses[str(parent_id)] == JobStatus.COMPLETED

    # Not `pause_dag`: nothing in the ancestry carries a flag, so there is no
    # operator state to lift and no expiry to wait for.
    assert dag.paused is False
    assert dag.paused_by is None
    assert dag.paused_expires_in_seconds is None

    # Not `create_stuck_dag`: nothing is dead-lettered, so there is nothing to
    # replay. With both contrasts excluded, escalating is the only correct move.
    assert dlq.total == 0
    assert dlq.items == []

    # Postgres returns this timezone-aware, from the same column the sweep
    # orders by — the whole reason this assertion is in this tier.
    child_node = next(n for n in dag.nodes if n.id == str(child_id))
    assert child_node.created_at.tzinfo is not None
    assert datetime.now(UTC) - child_node.created_at > timedelta(minutes=30)


async def test_the_same_window_promotes_the_child_when_the_sweep_is_not_paused(
    world: _World,
) -> None:
    """The control: same world and window, only the sweep's pause key absent, so
    a window too short for one pass cannot pass the test above by accident."""
    _, child_id = await world.seed_stranded_chain()
    redis = await _armed(pause_sweep=False)

    ticks = await _run_sweep_window(world.factory, redis)

    assert ticks >= _MIN_TICKS
    assert await world.status_of(child_id) == JobStatus.PENDING
    assert await world.submitted_events() == 1


async def test_the_pause_expiring_promotes_the_child_with_no_operator_action(
    world: _World,
) -> None:
    """The sweep's pause TTL, not the kill's, heals this world: the kill key is
    still set and the child is promoted anyway, because that `job.completed` is
    already consumed."""
    _, child_id = await world.seed_stranded_chain()
    redis = await _armed(pause_sweep=True)

    await _run_sweep_window(world.factory, redis)
    assert await world.status_of(child_id) == JobStatus.WAITING

    await redis.delete(_SWEEP_PAUSE_KEY)  # what the TTL does, on its own clock
    assert await redis.get(_RESOLVER_KILL_KEY) == "killed"

    await _run_sweep_window(world.factory, redis)
    assert await world.status_of(child_id) == JobStatus.PENDING, (
        "the sweep did not resume by itself once the flag went away"
    )
    assert await world.submitted_events() == 1
