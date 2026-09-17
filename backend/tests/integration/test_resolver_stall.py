"""Family C's world on a real Postgres: a child stranded `WAITING`, and nothing to fix.

Plan 01 §7.2 wants a world whose discriminator is an **absence** — a child stuck
`WAITING` with nothing dead-lettered and nothing paused — so that the correct
answer is to escalate rather than to replay (the `create_stuck_dag` contrast, a
dead-lettered root) or to un-pause (the `pause_dag` contrast, `paused=true` with
an expiry). That world needs BOTH of the platform's promoters stopped:

  * the `dependency-resolver` **consumer group**, which reacts to
    `job.completed`. `kill_consumer('dependency-resolver')` has stopped any
    consumer group by its group id since Wave 1 — it is a consumer group, not a
    tick loop, which is why it is deliberately not a `ControlLoopName`
    (divergence H2);
  * `_resume_unblocked_waiting_loop`, the resume **sweep**, which exists to
    "double as a backstop for any child whose promotion event was missed" and
    runs every `_RESUME_SWEEP_INTERVAL` = 10 s (divergence H3).
    `pause_control_loop('resume_unblocked_waiting')` stops it.

Stalling the resolver alone strands nothing: the sweep promotes the child within
about ten seconds, by design. Both stalls together are the packet, and pausing
the sweep is a deliberate suspension of a correctness backstop — bounded by a
TTL, swept by `make eval-reset`, and recorded in ADR 0027's 2026-09-17
amendment.

**Why this tier.** The world is the one a live scenario seeds against this
database, and the assertions are about what the server does with it. The sweep
selects promotable rows with a correlated `NOT EXISTS` over `job_dependencies`
and pages them behind a keyset cursor comparing the row value
`(created_at, id)` — row-value comparison and UUID binding that Postgres and the
SQLite harness do not implement alike. "The child has been waiting a long time"
is read from `created_at` as Postgres returns it, timezone-aware, from the same
column the sweep orders by. The unit twin,
`tests/unit/test_resolver_stall_world.py`, proves the pause *gate* (and carries
the `make eval-reset` key assertions, which are about a Redis pattern and need no
server); this file proves the *query* against the server that will run it.

**What is stubbed, and why that does not weaken it.** Redis, and Kafka by
omission. The two flags are single key lookups — `loop_is_paused` does one `GET`,
`find_blocking_pause` one `MGET`, `pause_state` one `TTL` — so a second container
would prove that redis-py can round-trip a string. Kafka is absent because the
defining property of this world is that the parent's `job.completed` is **already
in the past**: the resolver only ever reacts to that event, so there is no
delivery for a broker to make. The unit twin covers the other direction, where a
redelivery does arrive and the resolver promotes the child unless its kill key is
set.

Skipped automatically when Docker / testcontainers is unavailable, like every
other file in this tier.
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

#: The window, counted in sweep iterations rather than wall seconds. The real
#: interval is 10 s and no test should wait for two of those; the constant is
#: patched down and the loop then runs a fixed number of iterations, so the
#: window is a deterministic multiple of the sweep period instead of a
#: wall-clock guess a loaded machine can shorten. Twelve iterations is two
#: minutes of sweep time — twelve chances to promote the child, where one would
#: do.
_FAST_INTERVAL = 0.01
_MIN_TICKS = 12
_TICK_TIMEOUT = 20.0

#: How old the stranded child looks. 01 §7.2 lists "child created_at age large"
#: among the facts the agent reads: it is what separates a child the resolver has
#: not reached yet from one nothing is coming for.
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
        """Parent `COMPLETED`, child still `WAITING`, one dependency edge.

        Written at the data level rather than by completing the parent through
        the service, because the defining property is that the parent's
        `job.completed` has already happened. A world where that event is in the
        past is a world the resolver will never revisit — which is why the sweep
        exists, and exactly what pausing it takes away.
        """
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
        """`job.submitted` outbox rows — the announcement a promotion mints.

        Asserted beside the status because a promotion without the outbox add is
        the state the CAS in `promote_waiting_to_pending` produces for a *loser*,
        and a stalled world must produce neither half.
        """
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
    """The keys this world needs, behind the surface the real clients expose.

    `loop_is_paused` does a `GET` of the sweep's pause key, `find_blocking_pause`
    an `MGET` over `dag:paused:<ancestor>`, and `pause_state` a `TTL`.
    """

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
    """A fresh schema, one tenant, one user — torn down per test.

    Per-test rather than per-module because every test here promotes (or fails to
    promote) the same one row, and a leftover `PENDING` child would make the next
    test's assertion meaningless in the direction that matters.
    """
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
    """The lab's two keys, as a scenario's seeding step leaves them.

    The kill key is set in every case: it is what stops the resolver's poll loop
    (`BaseKafkaConsumer` checks `chaos:kill:<group>` at the top of every poll), it
    is the half this file does not exercise directly, and leaving it out of the
    control case would make the two windows differ in two ways instead of one.
    """
    redis = _Redis()
    await redis.set(_RESOLVER_KILL_KEY, "killed", ex=300)
    if pause_sweep:
        await redis.set(_SWEEP_PAUSE_KEY, "paused", ex=300)
    return redis


async def _run_sweep_window(
    factory: async_sessionmaker[AsyncSession], redis: _Redis
) -> int:
    """Run the real sweep loop for exactly `_MIN_TICKS` iterations.

    Three things are patched and nothing else:

    * `_RESUME_SWEEP_INTERVAL`, so the window is short in wall time while the
      loop body and its own `asyncio.sleep` stay untouched;
    * `CHAOS_ENABLED`, which is gate 1 — without it the pause check
      short-circuits before Redis and no key is ever read;
    * `dispatcher.loop_is_paused`, wrapped in a counter that **calls through to
      the real check**. The pause check runs every iteration where the promotion
      runs only on unpaused ones, so counting the check is the only honest
      measure of how long the window was.

    The loop is stopped by raising `CancelledError` out of that same check — the
    top of an iteration, before any database work — which is how the loop tests in
    `test_dispatcher.py` stop theirs. Cancelling the task from outside is not
    interchangeable: it can land inside the sweep's own transaction and leave the
    connection invalidated mid-statement.
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
    """The packet's assertion, and the agent's whole reading of it.

    Both promoters stopped: the resolver's kill key is set and there is no
    pending `job.completed` for it anyway, and the sweep's pause key is set. The
    child does not move across twelve sweep iterations — two minutes of sweep
    time — and every fact 01 §7.2 names comes back from the real read tools:
    parent `completed`, child `waiting`, `paused` false with `paused_by` null,
    `created_at` long past, and an empty DLQ.
    """
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
    """The control. Without it the test above proves nothing.

    Identical world, identical window, identical kill key — only the sweep's
    pause key is absent, and the child is promoted with its `job.submitted` row.
    This is what the world does with the hook removed, and it is the reason the
    window is counted in sweep iterations rather than in seconds: a window too
    short to contain one pass would pass the test above for the wrong reason.
    """
    _, child_id = await world.seed_stranded_chain()
    redis = await _armed(pause_sweep=False)

    ticks = await _run_sweep_window(world.factory, redis)

    assert ticks >= _MIN_TICKS
    assert await world.status_of(child_id) == JobStatus.PENDING
    assert await world.submitted_events() == 1


async def test_the_pause_expiring_promotes_the_child_with_no_operator_action(
    world: _World,
) -> None:
    """The TTL is the teardown, and it is the *only* thing that heals this world.

    Deleting the key and letting Redis expire it are indistinguishable to the
    check, which is a `GET`, and waiting out a real TTL would put a minimum sleep
    in the tier for no extra coverage. The half that matters is the asymmetry:
    the resolver's kill key is still set here and the child is promoted anyway,
    because the resolver was never going to promote it — the `job.completed` that
    would have is already consumed. The sweep resuming is what un-stalls the
    world, which is why the sweep's pause TTL, not the kill's, bounds the fault.
    """
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
