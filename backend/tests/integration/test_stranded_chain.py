"""The manufactured stranded chain on a real Postgres, against the real sweep.

WO-R3-274 + WO-R3-275. `tests/integration/test_resolver_stall.py` proved that a
chain in this shape, written row by row, is held by the two stalls; this file
proves that the shape the **hook** writes is the same shape, by feeding it to
the same sweep — and that the two claims the hook makes about itself are true of
the server that will run it:

  * `root_status="completed"` does NOT hold on its own. The root being
    `completed` leaves step-1 with no unmet parent, so the resume sweep promotes
    it. Asserted in the direction that hurts: the chain drains in an unpaused
    window, and only holds when `pause_control_loop('resume_unblocked_waiting')`
    is set. A hook that quietly stayed stuck here would mean the description is
    wrong, and the fault would evaporate in a live run instead.
  * `failed_step` DOES hold on its own, with no stall at all, because no
    `waiting` row in it has a `completed` parent.

And the third world: a `pause_dag_chaos` pause is **enforced**, not just
reported. The sweep is what enforces it (ADR 0011 via ADR 0022), so the proof is
an unpaused sweep window declining to promote a chain the lab paused — which
takes the real keyset query, the real `NOT EXISTS` over `job_dependencies` and
the real `find_blocking_pause` ancestor walk. That is why this tier.

**Why the hooks are called directly rather than over the wire.** The API twin
(`tests/api/test_mcp_stranded_chain_and_lab_pause.py`) already exercises the
JSON-RPC envelope, the scopes and the gating. What only Postgres can answer is
what its own planner and clock do with the rows — `created_at` returned
timezone-aware from the column the sweep orders by, the row-value keyset
comparison, and the correlated subquery. Under the unit tier's default
`CHAOS_ENABLED=false` the `@chaos_tool` decorator is a no-op that returns the
function unchanged, so the handler is importable and callable here without
opening the gate.

Redis is stubbed, as in `test_resolver_stall.py`: every flag this world needs is
one key lookup (`GET`, `MGET`, `TTL`), so a second container would prove that
redis-py round-trips a string.

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
from app.config import Settings
from app.core.scopes import Scope
from app.dependencies import Principal
from app.mcp.registry import ToolContext
from app.mcp.tools.chaos.create_stuck_dag import (
    CreateStuckDagInput,
    create_stuck_dag,
)
from app.mcp.tools.chaos.pause_dag_chaos import (
    PauseDagChaosInput,
    pause_dag_chaos,
)
from app.mcp.tools.dag_state import GetDagStateInput, get_dag_state
from app.mcp.tools.list_dlq_messages import (
    ListDlqMessagesInput,
    list_dlq_messages,
)
from app.mcp.tools.traces import SearchTracesInput, search_traces
from app.models.base import Base
from app.models.enums import JobStatus, UserRole
from app.models.job import Job
from app.models.job_dependency import JobDependency
from app.models.outbox import OutboxEvent
from app.models.saga import Saga
from app.models.tenant import Tenant
from app.models.triage import JobTriage
from app.models.user import User
from app.utils.dag_pause import pause_key_for as dag_pause_key_for
from app.workers import dispatcher
from app.workers.control_loop_pause import (
    ControlLoopName,
    loop_is_paused,
    pause_key_for,
)
from sqlalchemy import select
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

_SWEEP_PAUSE_KEY = pause_key_for(ControlLoopName.RESUME_UNBLOCKED_WAITING)

#: The window, counted in sweep iterations rather than wall seconds — the same
#: discipline `test_resolver_stall.py` established, and for the same reason: a
#: window too short to contain one pass would pass the "it held" assertions for
#: the wrong reason.
_FAST_INTERVAL = 0.01
_MIN_TICKS = 12
_TICK_TIMEOUT = 20.0

#: What the hook is asked to backdate the chain by. Plan 01 §7.2 reads "child
#: created_at age large"; the platform's own stranded-child proof uses 47 min.
_CHILD_AGE_SECONDS = 47 * 60

_TABLES = [
    Tenant.__table__,
    User.__table__,
    Saga.__table__,
    Job.__table__,
    JobDependency.__table__,
    OutboxEvent.__table__,
    JobTriage.__table__,
]


class _Redis:
    """The keys this world needs, behind the surface the real clients expose."""

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

    def keys_matching(self, prefix: str) -> list[str]:
        return sorted(k for k in self._store if k.startswith(prefix))


class _World:
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

    def ctx(self, session: AsyncSession, redis: Any, *, scope: Scope) -> ToolContext:
        return ToolContext(
            db=session,
            redis=redis,
            principal=Principal(
                kind="service_account",
                tenant_id=self.tenant_id,
                scopes=frozenset({scope.value}),
            ),
        )

    async def manufacture(self, redis: _Redis, **kwargs: Any) -> Any:
        """Run the chaos hook against the real database, in its own transaction.

        `chain_name` is per-call so two chains can coexist, which is what the
        pause comparison needs.
        """
        async with self.factory() as session:
            async with session.begin():
                made = await create_stuck_dag(
                    CreateStuckDagInput(**kwargs),
                    self.ctx(session, redis, scope=Scope.CHAOS_INVOKE),
                )
        return made

    async def status_of(self, job_id: str) -> str:
        async with self.factory() as session:
            return (
                await session.execute(
                    select(Job.status).where(Job.id == uuid.UUID(job_id))
                )
            ).scalar_one()

    async def read(self, redis: _Redis, job_id: str) -> Any:
        async with self.factory() as session:
            return await get_dag_state(
                GetDagStateInput(job_id=uuid.UUID(job_id)),
                self.ctx(session, redis, scope=Scope.INCIDENTS_READ),
            )

    async def waiting_traces(self, redis: _Redis) -> Any:
        async with self.factory() as session:
            return await search_traces(
                SearchTracesInput(status=JobStatus.WAITING.value),
                self.ctx(session, redis, scope=Scope.INCIDENTS_READ),
            )

    async def dlq(self, redis: _Redis) -> Any:
        async with self.factory() as session:
            return await list_dlq_messages(
                ListDlqMessagesInput(),
                self.ctx(session, redis, scope=Scope.INCIDENTS_READ),
            )


@pytest.fixture(scope="module")
def pg() -> Any:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


@pytest.fixture
async def world(pg: Any) -> AsyncIterator[_World]:
    """A fresh schema, one tenant, one user — torn down per test, because every
    test here promotes (or fails to promote) rows with the same derived ids."""
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
                    name="stranded chain",
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


async def _run_sweep_window(
    factory: async_sessionmaker[AsyncSession], redis: _Redis
) -> int:
    """Run the real resume sweep for exactly `_MIN_TICKS` iterations.

    Lifted from `test_resolver_stall.py`, including why each patch is there:
    the interval so the window is short in wall time, `CHAOS_ENABLED` so the
    pause check reaches Redis at all, and a counting wrapper that calls through
    to the real check (the pause check runs every iteration; the promotion runs
    only on unpaused ones, so counting the check is the honest measure). The
    loop is stopped by raising `CancelledError` out of that check — at the top
    of an iteration, before any database work — because cancelling the task from
    outside can land inside the sweep's own transaction.
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


# ---------------------------------------------------------------------------
# resolver_stall — the hook writes the shape, the two stalls hold it
# ---------------------------------------------------------------------------


async def test_the_stranded_chain_holds_while_the_sweep_is_paused(
    world: _World,
) -> None:
    """The world plan 01 §7.2 calls `resolver_stall`, manufactured end to end.

    The resolver is out of the picture by construction here, as it is live: the
    root's `job.completed` is in the past (these rows were inserted already
    `completed`), so there is no delivery for it to react to. With the resume
    sweep paused, nothing promotes — and every fact the plan lists comes back
    from the agent's own read tools, including a `created_at` that Postgres
    returns timezone-aware from the column the sweep orders by.
    """
    redis = _Redis()
    await redis.set(_SWEEP_PAUSE_KEY, "paused", ex=300)

    made = await world.manufacture(
        redis,
        chain_name="stranded",
        root_status="completed",
        child_age_seconds=_CHILD_AGE_SECONDS,
    )
    assert made.dead_letter_job_id is None

    ticks = await _run_sweep_window(world.factory, redis)
    assert ticks >= _MIN_TICKS

    for step_id in made.step_job_ids:
        assert await world.status_of(step_id) == JobStatus.WAITING.value, (
            "the paused sweep promoted a descendant anyway"
        )
    assert await world.status_of(made.root_job_id) == JobStatus.COMPLETED.value

    dag = await world.read(redis, made.waiting_job_ids[0])
    statuses = {node.id: node.status for node in dag.nodes}
    assert statuses[made.waiting_job_ids[0]] == JobStatus.WAITING.value
    assert statuses[made.root_job_id] == JobStatus.COMPLETED.value
    # Not `pause_dag`: nothing in the ancestry carries a flag.
    assert dag.paused is False
    assert dag.paused_by is None
    assert dag.paused_expires_in_seconds is None

    # Not `create_stuck_dag`'s old chain: nothing to replay.
    dlq = await world.dlq(redis)
    assert dlq.total == 0
    assert dlq.items == []

    # The backdate, as the server stored and returned it.
    traces = await world.waiting_traces(redis)
    matched = {m.job_id: m for m in traces.matches}
    for step_id in made.step_job_ids:
        assert step_id in matched, "a stranded descendant is not searchable"
        assert matched[step_id].created_at.tzinfo is not None
        age = datetime.now(UTC) - matched[step_id].created_at
        assert timedelta(minutes=45) < age < timedelta(minutes=50)


async def test_the_same_window_drains_the_stranded_chain_when_the_sweep_runs(
    world: _World,
) -> None:
    """The control, and the description's honesty claim in one test.

    Identical chain, identical window, no sweep pause — and step-1 is promoted,
    because a `completed` root leaves it with no unmet parent. This is what
    makes "`root_status='completed'` does NOT hold by itself" a fact about the
    platform rather than a caution in a docstring, and it is why the tool
    description names both companion hooks.

    Only step-1 moves: step-2 is still waiting on step-1, which is now `pending`
    rather than `completed`.
    """
    redis = _Redis()  # no sweep pause

    made = await world.manufacture(
        redis,
        chain_name="drains",
        root_status="completed",
        waiting_steps=2,
        child_age_seconds=_CHILD_AGE_SECONDS,
    )

    ticks = await _run_sweep_window(world.factory, redis)
    assert ticks >= _MIN_TICKS

    first, second = made.step_job_ids
    assert await world.status_of(first) == JobStatus.PENDING.value, (
        "the completed-root chain held without either stall — the tool "
        "description promises it does not, so one of the two is now wrong"
    )
    assert await world.status_of(second) == JobStatus.WAITING.value


async def test_the_default_dead_lettered_chain_still_holds_with_no_stall(
    world: _World,
) -> None:
    """The shape this hook has always written, unchanged by the new inputs.

    `dead_letter` is terminal and the sweep only promotes a row whose parents
    are all `completed`, so twelve sweep passes move nothing. Asserted here
    rather than trusted, because `root_status` is the first thing to touch this
    chain's statuses since it shipped.
    """
    redis = _Redis()

    made = await world.manufacture(redis, chain_name="default-shape")
    assert made.dead_letter_job_id == made.root_job_id

    await _run_sweep_window(world.factory, redis)

    assert await world.status_of(made.root_job_id) == JobStatus.DEAD_LETTER.value
    for step_id in made.step_job_ids:
        assert await world.status_of(step_id) == JobStatus.WAITING.value


# ---------------------------------------------------------------------------
# downstream_child_failed — holds with no stall at all
# ---------------------------------------------------------------------------


async def test_the_failed_step_chain_holds_on_its_own_with_one_dlq_row(
    world: _World,
) -> None:
    """`downstream_child_failed`, and the claim that it needs no companion hook.

    No `waiting` row in this chain has a `completed` parent — step-1 is itself
    `completed`, step-2 is `dead_letter`, and step-3 waits on a terminal row —
    so an unpaused sweep window promotes nothing. The DLQ shows exactly one
    entry and it is the descendant, not the root: the root `completed`, which
    is the whole difference from the chain this hook used to be limited to.
    """
    redis = _Redis()  # deliberately no stall of any kind

    made = await world.manufacture(
        redis,
        chain_name="downstream",
        root_status="completed",
        waiting_steps=3,
        failed_step=2,
        remediation_hint="human_required",
        child_age_seconds=_CHILD_AGE_SECONDS,
    )
    first, failed, last = made.step_job_ids
    assert made.dead_letter_job_id == failed
    assert made.waiting_job_ids == [last]

    ticks = await _run_sweep_window(world.factory, redis)
    assert ticks >= _MIN_TICKS

    assert await world.status_of(made.root_job_id) == JobStatus.COMPLETED.value
    assert await world.status_of(first) == JobStatus.COMPLETED.value
    assert await world.status_of(failed) == JobStatus.DEAD_LETTER.value
    assert await world.status_of(last) == JobStatus.WAITING.value, (
        "an unpaused sweep promoted a child waiting on a dead-lettered parent"
    )

    dlq = await world.dlq(redis)
    assert dlq.total == 1
    assert [item.id for item in dlq.items] == [failed]
    assert dlq.items[0].remediation_hint == "human_required"


# ---------------------------------------------------------------------------
# paused_dag — the lab pause is enforced by the sweep, not merely reported
# ---------------------------------------------------------------------------


async def test_the_lab_pause_is_enforced_by_the_real_sweep(
    world: _World,
) -> None:
    """The pair the plan wants: identical node statuses, one boolean apart.

    Same chain as `test_the_same_window_drains_…`, same unpaused window — and
    step-1 is NOT promoted, because `pause_dag_chaos` wrote the flag the sweep
    itself checks through `find_blocking_pause`. That is the difference between
    a pause that is reported and a pause that is real, and it is only provable
    against the server that runs the ancestor walk and the keyset query.
    """
    redis = _Redis()  # the sweep runs; only the DAG is paused

    made = await world.manufacture(
        redis,
        chain_name="lab-paused",
        root_status="completed",
        waiting_steps=2,
        child_age_seconds=_CHILD_AGE_SECONDS,
    )

    async with world.factory() as session:
        async with session.begin():
            paused = await pause_dag_chaos(
                PauseDagChaosInput(root_job_id=uuid.UUID(made.root_job_id)),
                world.ctx(session, redis, scope=Scope.CHAOS_INVOKE),
            )
    assert paused.pause_key == dag_pause_key_for(made.root_job_id)
    assert paused.ttl_seconds == 600

    ticks = await _run_sweep_window(world.factory, redis)
    assert ticks >= _MIN_TICKS

    first, second = made.step_job_ids
    assert await world.status_of(first) == JobStatus.WAITING.value, (
        "the sweep promoted a child inside a paused DAG — the lab pause is "
        "being reported but not enforced"
    )
    assert await world.status_of(second) == JobStatus.WAITING.value

    # And it reads the way an operator pause reads.
    root_view = await world.read(redis, made.root_job_id)
    child_view = await world.read(redis, first)
    assert root_view.paused is True
    assert root_view.paused_expires_in_seconds == 600
    assert root_view.paused_by == made.root_job_id
    assert child_view.paused is False
    assert child_view.paused_by == made.root_job_id

    # The one key it wrote is the platform's, not a chaos-namespaced copy —
    # which is why `_clear_dag_pauses`, not the `chaos:*` scan, is its teardown.
    assert redis.keys_matching("dag:paused:") == [paused.pause_key]
    assert redis.keys_matching("chaos:") == []


async def test_the_lab_pause_lapsing_lets_the_same_window_drain_the_chain(
    world: _World,
) -> None:
    """The TTL is the teardown and nothing has to be called.

    Deleting the key and letting Redis expire it are indistinguishable to the
    check (a `GET` inside an `MGET`), and waiting out a real TTL would put a
    minimum sleep in this tier for no extra coverage. What is worth proving is
    that the world heals *by itself* afterwards — the same window that held
    while the flag was set now promotes step-1, so a lab pause cannot outlive
    its scenario.
    """
    redis = _Redis()

    made = await world.manufacture(
        redis,
        chain_name="lapsing",
        root_status="completed",
        waiting_steps=2,
    )
    async with world.factory() as session:
        async with session.begin():
            await pause_dag_chaos(
                PauseDagChaosInput(
                    root_job_id=uuid.UUID(made.root_job_id), ttl_seconds=30
                ),
                world.ctx(session, redis, scope=Scope.CHAOS_INVOKE),
            )

    await _run_sweep_window(world.factory, redis)
    first = made.step_job_ids[0]
    assert await world.status_of(first) == JobStatus.WAITING.value

    await redis.delete(*redis.keys_matching("dag:paused:"))  # what the TTL does

    await _run_sweep_window(world.factory, redis)
    assert await world.status_of(first) == JobStatus.PENDING.value, (
        "the sweep did not resume promoting once the pause flag went away"
    )
