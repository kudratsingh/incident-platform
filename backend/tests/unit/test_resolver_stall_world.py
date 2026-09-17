"""Family C's world: a child stranded `WAITING` because BOTH promoters are stopped.

Plan 01 §7.2 asks for a world whose discriminator is an *absence* — a child stuck
`WAITING` with nothing dead-lettered and nothing paused — so that the correct
answer is to escalate rather than to replay (`create_stuck_dag`) or to un-pause
(`pause_dag`). Two things promote a `WAITING` child whose parents have all
reached `COMPLETED`, and that world exists only while both are stopped:

  * the `dependency-resolver` **consumer group**, which reacts to
    `job.completed`. `kill_consumer('dependency-resolver')` has stopped any
    consumer group since Wave 1 — it is a consumer group and not a tick loop,
    which is why it is deliberately absent from `ControlLoopName`
    (divergence H2, ADR 0027);
  * `_resume_unblocked_waiting_loop`, the resume **sweep**, which exists to
    backstop exactly this and runs every `_RESUME_SWEEP_INTERVAL` = 10 s
    (divergence H3). `pause_control_loop('resume_unblocked_waiting')` stops it.

Stalling the resolver alone strands nothing: the sweep promotes the child within
about ten seconds, by design. That is the whole packet, and the pair of tests
that carries it is `…holds_the_child…` plus `…the_same_window_promotes…` — the
paused window holds the child, and *the same window* promotes it when the sweep
is not paused. The control is what makes the first assertion mean anything: a
window too short to contain a sweep pass would pass just as happily with the
pause removed, which is the failure the packet's test requirement names.

Real rows on a real (SQLite in-memory) engine, following
`test_resume_sweep_promotable.py` rather than the mock-heavy `test_dispatcher.py`
style: every claim here is about what the sweep's own SQL does to a row, and
about what the agent's read tools say afterwards. `tests/integration/
test_resolver_stall.py` makes the same claim against the Postgres a live lab runs
on, where `now()` and `created_at` are the server's.

Windows are counted in **sweep iterations, not wall seconds.** The loop's real
interval is 10 s and no test should wait for two of those; the interval constant
is patched down and the loop is then run until it has been observed to iterate
`_MIN_TICKS` times, so the window is a deterministic multiple of the sweep
period rather than a wall-clock guess that a loaded machine can shorten.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest_asyncio
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
from app.models.tenant import DEFAULT_TENANT_ID, Tenant
from app.models.user import User
from app.utils.dag_pause import pause_key_for as dag_pause_key_for
from app.workers import dispatcher
from app.workers.control_loop_pause import (
    ControlLoopName,
    loop_is_paused,
    pause_key_for,
)
from app.workers.dependency_resolver import DependencyResolver
from app.workers.kafka_consumer import _check_chaos_kill, kill_key_for
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

# Mixed hex on purpose, for the reason `DEFAULT_TENANT_ID` is: an all-digit UUID
# hex round-trips through SQLite's NUMERIC affinity as a float and blows up the
# UUID result processor.
_USER_ID = uuid.UUID("b71e5c48-2a0d-4e93-8f16-5d27c0a9e4b3")

#: The pause the packet adds, and the kill it composes with. Both derived from
#: the shipped helpers so a rename cannot leave this file testing a dead key.
_SWEEP_PAUSE_KEY = pause_key_for(ControlLoopName.RESUME_UNBLOCKED_WAITING)
_RESOLVER_GROUP = get_settings().kafka_consumer_group_dependency
_RESOLVER_KILL_KEY = kill_key_for(_RESOLVER_GROUP)

#: Patched in for the window, and how many iterations the window must contain.
#: Twelve iterations of the real loop is two minutes of sweep time — comfortably
#: more than the one pass that would promote the child — and the wait is on the
#: count, not on the clock, so a slow machine lengthens the window instead of
#: silently shortening it.
_FAST_INTERVAL = 0.01
_MIN_TICKS = 12
_TICK_TIMEOUT = 15.0

#: How old the stranded child looks. Plan 01 §7.2 lists "child created_at age
#: large" among the facts the agent reads, and it is what separates a child the
#: resolver has not got to yet from one nothing is coming for.
_CHILD_AGE = timedelta(minutes=47)


class _Redis:
    """The keys this world needs, behind the surface the real clients expose.

    Three readers share it and each wants something different: `loop_is_paused`
    does a `GET` of the sweep's pause key, `find_blocking_pause` does an `MGET`
    over `dag:paused:<ancestor>`, and `pause_state` does a `TTL`. A stub rather
    than a container because every one of those is a single key lookup.
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


@pytest_asyncio.fixture
async def session_factory() -> AsyncGenerator[  # type: ignore[return]
    async_sessionmaker[AsyncSession], None
]:
    """A module-local engine, so committed rows never leak into other suites."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            session.add(
                Tenant(
                    id=DEFAULT_TENANT_ID,
                    slug="default",
                    name="Default Tenant",
                    is_active=True,
                )
            )
            session.add(
                User(
                    id=_USER_ID,
                    tenant_id=DEFAULT_TENANT_ID,
                    email="owner@example.com",
                    hashed_password="not-a-real-hash",
                    role=UserRole.USER,
                    is_active=True,
                )
            )
    try:
        yield factory
    finally:
        await engine.dispose()


def _job(*, status: str, created_at: datetime) -> Job:
    return Job(
        id=uuid.uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        user_id=_USER_ID,
        type=JobType.CSV_UPLOAD,
        status=status,
        payload={"rows": 1},
        max_attempts=3,
        created_at=created_at,
        updated_at=created_at,
    )


async def _seed_stranded_chain(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID]:
    """The world 01 §7.2 describes: parent `COMPLETED`, child still `WAITING`.

    Inserted at the data level rather than driven through a completion, because
    the defining property is that the parent's `job.completed` has *already
    happened*. The resolver only ever reacts to that event, so a world where the
    event is in the past is a world the resolver will never revisit — which is
    exactly why the sweep exists, and exactly what the pause takes away.
    """
    created = datetime.now(UTC) - _CHILD_AGE
    async with factory() as session:
        async with session.begin():
            parent = _job(status=JobStatus.COMPLETED, created_at=created)
            child = _job(status=JobStatus.WAITING, created_at=created)
            session.add_all([parent, child])
            session.add(
                JobDependency(job_id=child.id, depends_on_job_id=parent.id)
            )
    return parent.id, child.id


async def _status_of(
    factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> str:
    async with factory() as session:
        return (
            await session.execute(select(Job.status).where(Job.id == job_id))
        ).scalar_one()


async def _submitted_events(factory: async_sessionmaker[AsyncSession]) -> int:
    """`job.submitted` outbox rows — the announcement a promotion mints.

    Asserted beside the status because promoting the row without the outbox add
    is a state the CAS in `promote_waiting_to_pending` is designed to produce
    for a *loser*, and a stalled world must produce neither half.
    """
    async with factory() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.topic == "job.submitted")
            )
        ).scalar_one()


async def _run_sweep_window(
    factory: async_sessionmaker[AsyncSession], redis: _Redis
) -> int:
    """Run the real sweep loop for exactly `_MIN_TICKS` iterations.

    Returns the iteration count. Three things are patched and nothing else:

    * `_RESUME_SWEEP_INTERVAL`, so the window is short in wall time while the
      loop body and its own `asyncio.sleep` stay untouched;
    * `CHAOS_ENABLED`, which is gate 1 — without it the pause check
      short-circuits before Redis and no key is ever read;
    * `dispatcher.loop_is_paused`, wrapped in a counter that **calls through to
      the real check**. Counting iterations from inside the loop is the only
      honest way to say how long the window was: the pause check happens every
      iteration, where the promotion happens only on the iterations that are not
      paused.

    The loop is stopped by raising `CancelledError` out of that same check, the
    way the loop tests in `test_dispatcher.py` and `test_pause_control_loop.py`
    stop theirs — the top of an iteration, before any database work. Cancelling
    the task from outside is *not* interchangeable here: it can land inside the
    sweep's transaction, and an aiosqlite connection invalidated mid-statement is
    replaced by a reconnect, which for an in-memory database is a fresh and empty
    one. The symptom is `no such table: jobs` from the assertion afterwards.
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


def _ctx(db: AsyncSession, redis: Any) -> ToolContext:
    """The agent's own context: a read-scoped machine principal in one tenant."""
    return ToolContext(
        db=db,
        redis=redis,
        principal=Principal(
            kind="service_account",
            tenant_id=DEFAULT_TENANT_ID,
            scopes=frozenset(
                {Scope.TELEMETRY_READ.value, Scope.INCIDENTS_READ.value}
            ),
        ),
    )


# ---------------------------------------------------------------------------
# The composed stall, and the control that gives it meaning
# ---------------------------------------------------------------------------


async def test_the_composed_stall_holds_the_child_across_many_sweep_ticks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Both promoters stopped, and the child does not move.

    The sweep is paused by its own key and the resolver has nothing to deliver
    (the parent's completion is in the past), so nothing promotes the child for
    as long as the pause holds. The tick count is asserted because the absence
    of a promotion is only evidence if the sweep had the chance to promote.
    """
    _, child_id = await _seed_stranded_chain(session_factory)
    redis = _Redis()
    await redis.set(_SWEEP_PAUSE_KEY, "paused", ex=300)

    ticks = await _run_sweep_window(session_factory, redis)

    assert ticks >= _MIN_TICKS
    assert await _status_of(session_factory, child_id) == JobStatus.WAITING, (
        "the paused sweep promoted the child anyway"
    )
    assert await _submitted_events(session_factory) == 0, (
        "the paused sweep announced a promotion it did not make"
    )


async def test_the_same_window_promotes_the_child_when_the_sweep_is_not_paused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The control. Without it the test above proves nothing.

    Identical world, identical window, no pause key — and the child is promoted
    with its `job.submitted` row. This is the red half of the packet: it is what
    the world does with the hook absent, and the reason a window has to be
    counted in sweep iterations rather than in seconds.
    """
    _, child_id = await _seed_stranded_chain(session_factory)
    redis = _Redis()  # no pause key

    ticks = await _run_sweep_window(session_factory, redis)

    assert ticks >= _MIN_TICKS
    assert await _status_of(session_factory, child_id) == JobStatus.PENDING
    assert await _submitted_events(session_factory) == 1


async def test_the_pause_expiring_promotes_the_child_with_no_operator_action(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The TTL is the teardown, and it is the *only* thing that heals this world.

    Deleting the key and letting Redis expire it are indistinguishable to the
    check, which is a `GET`, so the key is deleted rather than waited out. What
    matters is the asymmetry: the resolver coming back does not rescue the child,
    because the `job.completed` that would have promoted it has already been
    consumed. The sweep resuming is what un-stalls the world, which is why the
    sweep's pause TTL — not the kill's — bounds the fault.
    """
    _, child_id = await _seed_stranded_chain(session_factory)
    redis = _Redis()
    await redis.set(_SWEEP_PAUSE_KEY, "paused", ex=300)

    await _run_sweep_window(session_factory, redis)
    assert await _status_of(session_factory, child_id) == JobStatus.WAITING

    await redis.delete(_SWEEP_PAUSE_KEY)  # what the TTL does, on its own clock

    await _run_sweep_window(session_factory, redis)
    assert await _status_of(session_factory, child_id) == JobStatus.PENDING, (
        "the sweep did not resume by itself once the flag went away"
    )
    assert await _submitted_events(session_factory) == 1


# ---------------------------------------------------------------------------
# Why it takes both: the resolver is the other promoter
# ---------------------------------------------------------------------------


async def test_pausing_the_sweep_alone_leaves_the_resolver_promoting(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Divergence H3 from the other side.

    A `job.completed` redelivery reaches a resolver that is still polling, and
    the child is promoted with the sweep's pause still set. So the pause is
    necessary and not sufficient: a scenario that paused only the sweep would
    hold the child until the next delivery on `job.completed` and then lose it,
    intermittently and for a reason no reading would explain.
    """
    parent_id, child_id = await _seed_stranded_chain(session_factory)
    redis = _Redis()
    await redis.set(_SWEEP_PAUSE_KEY, "paused", ex=300)

    resolver = DependencyResolver(session_factory, redis)
    await resolver.handle_message(
        "job.completed",
        f"{DEFAULT_TENANT_ID}:{_USER_ID}",
        {"event": "job.completed", "job_id": str(parent_id)},
    )

    assert await _status_of(session_factory, child_id) == JobStatus.PENDING
    assert await redis.get(_SWEEP_PAUSE_KEY) == "paused"


async def test_the_resolver_is_stopped_by_its_own_kill_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The other half of the composed stall, at the check that performs it.

    `BaseKafkaConsumer` calls `_check_chaos_kill(self.group_id)` at the top of
    every poll and shuts down when it answers true, so a killed group never
    reaches `handle_message` — which is the delivery the test above shows would
    otherwise undo the stall. Asserted through the group id the resolver is
    actually constructed with, so `kill_consumer` cannot be called with a name
    no consumer answers to.
    """
    resolver = DependencyResolver(session_factory)
    assert resolver.group_id == _RESOLVER_GROUP

    redis = _Redis()
    with patch("app.core.redis.get_redis_client", return_value=redis):
        assert await _check_chaos_kill(resolver.group_id) is False
        await redis.set(_RESOLVER_KILL_KEY, "killed", ex=300)
        assert await _check_chaos_kill(resolver.group_id) is True


# ---------------------------------------------------------------------------
# What the agent reads: an absence, and two contrasts
# ---------------------------------------------------------------------------


async def test_the_stalled_world_reads_as_waiting_with_nothing_paused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The facts plan 01 §7.2 asks the agent to reason from, from the real tools.

    `get_dag_state` on the child: the parent is `completed`, the child is
    `waiting`, `paused` is false and `paused_by` is null, and the child's
    `created_at` is long past. `list_dlq_messages` is empty. Nothing to replay
    and nothing to un-pause is the whole discriminator — the correct answer is
    to escalate, so the reading has to support that and nothing else.
    """
    parent_id, child_id = await _seed_stranded_chain(session_factory)
    redis = _Redis()
    await redis.set(_SWEEP_PAUSE_KEY, "paused", ex=300)
    await _run_sweep_window(session_factory, redis)

    async with session_factory() as session:
        dag = await get_dag_state(
            GetDagStateInput(job_id=child_id), _ctx(session, redis)
        )
        dlq = await list_dlq_messages(
            ListDlqMessagesInput(), _ctx(session, redis)
        )

    statuses = {node.id: node.status for node in dag.nodes}
    assert statuses[str(child_id)] == JobStatus.WAITING
    assert statuses[str(parent_id)] == JobStatus.COMPLETED

    # Not `pause_dag`: no flag anywhere in the ancestry.
    assert dag.paused is False
    assert dag.paused_by is None
    assert dag.paused_expires_in_seconds is None

    # Not `create_stuck_dag`: nothing dead-lettered to replay.
    assert dlq.total == 0
    assert dlq.items == []

    # The child has been waiting a long time, which is what makes it a fault
    # rather than a promotion that has not happened yet.
    child_node = next(n for n in dag.nodes if n.id == str(child_id))
    created = child_node.created_at
    if created.tzinfo is None:
        # SQLite has no timezone-aware type and hands the offset back stripped.
        # Postgres does not, which is one of the things the integration-tier
        # twin of this file exists to read against the server's own clock.
        created = created.replace(tzinfo=UTC)
    assert datetime.now(UTC) - created > timedelta(minutes=30)


async def test_a_paused_dag_reads_differently_so_the_null_is_a_real_reading(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Anti-vacuity, the lesson INC-001 cost a paid run to learn.

    `paused_by is None` only distinguishes this world from `pause_dag`'s if the
    field can be non-null on the same read path. Same chain, same tool, one
    `dag:paused:<parent>` flag — and the ancestor's id comes back. Without this
    the assertions above would keep passing if the pause lookup broke.
    """
    parent_id, child_id = await _seed_stranded_chain(session_factory)
    redis = _Redis()
    await redis.set(dag_pause_key_for(parent_id), "1", ex=600)

    async with session_factory() as session:
        dag = await get_dag_state(
            GetDagStateInput(job_id=child_id), _ctx(session, redis)
        )

    assert dag.paused is False  # the child carries no flag of its own
    assert dag.paused_by == str(parent_id)


async def test_the_sweep_pause_does_not_reach_the_other_loops(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`single_loop` is a blast-radius claim, so the reverse gets an assertion.

    A key for a different member must not stop this sweep — the failure a shared
    key or a prefix match would produce, and one that would silently widen every
    scenario's fault.
    """
    _, child_id = await _seed_stranded_chain(session_factory)
    redis = _Redis()
    await redis.set(
        pause_key_for(ControlLoopName.STALE_RUNNING_SWEEP), "paused", ex=300
    )

    await _run_sweep_window(session_factory, redis)

    assert await _status_of(session_factory, child_id) == JobStatus.PENDING, (
        "pausing another loop stopped the resume sweep"
    )


