"""Resume sweep starvation and the dependency cascade (R2-09).

Real rows on a real (SQLite in-memory) engine rather than the mock-heavy
`test_dispatcher.py` style: the fix is a `NOT EXISTS` subquery, an ORDER BY
and a keyset cursor, and a mocked session proves nothing about any of them.

The engine is module-local so committed rows never leak into the shared
session-scoped `sqlite_engine` other suites roll back against.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from app.models.base import Base
from app.models.enums import JobStatus, JobType, UserRole
from app.models.job import Job
from app.models.job_dependency import JobDependency
from app.models.outbox import OutboxEvent
from app.models.saga import Saga
from app.models.tenant import DEFAULT_TENANT_ID, Tenant
from app.models.user import User
from app.repositories.job import JobRepository
from app.workers.dispatcher import (
    _RESUME_SWEEP_LIMIT,
    _resume_unblocked_waiting_once,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

# Mixed hex on purpose, same reason `DEFAULT_TENANT_ID` is: an all-digit UUID
# hex round-trips through SQLite's NUMERIC affinity as a float and blows up
# the UUID result processor.
_USER_ID = uuid.UUID("c4b5a697-8d9e-4f01-9a2b-3c4d5e6f7a8b")

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class _StubRedis:
    """Only the surface `find_blocking_pause` touches.

    `paused` holds the job ids whose DAG-pause flag is set; everything else
    reads back as an absent key.
    """

    def __init__(self, paused: set[uuid.UUID] | None = None) -> None:
        self.paused = {str(j) for j in (paused or set())}

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [
            "1" if k.removeprefix("dag:paused:") in self.paused else None
            for k in keys
        ]


@pytest_asyncio.fixture
async def session_factory() -> AsyncGenerator[  # type: ignore[return]
    async_sessionmaker[AsyncSession], None
]:
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


def _job(
    *,
    status: str,
    created_at: datetime,
    saga_id: uuid.UUID | None = None,
) -> Job:
    return Job(
        id=uuid.uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        user_id=_USER_ID,
        type=JobType.CSV_UPLOAD,
        status=status,
        payload={"rows": 1},
        max_retries=3,
        saga_id=saga_id,
        created_at=created_at,
        updated_at=created_at,
    )


async def _status_of(
    factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> str:
    async with factory() as session:
        return (
            await session.execute(select(Job.status).where(Job.id == job_id))
        ).scalar_one()


async def _seed_blocked_wall(
    factory: async_sessionmaker[AsyncSession], count: int
) -> None:
    """`count` WAITING children whose parent is DEAD_LETTER — the stuck set.

    Inserted directly rather than by dead-lettering a parent through
    `update_status`, because that path now cascades them to CANCELLED. Direct
    inserts are how this state actually accumulated in production (and how
    the `create_stuck_dag` chaos hook manufactures it), so this is the real
    backlog shape, not a synthetic one.

    Created oldest-first so an unordered `LIMIT 200` — which on SQLite scans
    in insertion order — sees the wall before anything seeded after it.
    """
    async with factory() as session:
        async with session.begin():
            for i in range(count):
                parent = _job(
                    status=JobStatus.DEAD_LETTER,
                    created_at=_EPOCH + timedelta(seconds=i),
                )
                child = _job(
                    status=JobStatus.WAITING,
                    created_at=_EPOCH + timedelta(seconds=i),
                )
                session.add_all([parent, child])
                session.add(
                    JobDependency(
                        job_id=child.id, depends_on_job_id=parent.id
                    )
                )


async def test_healthy_child_promoted_from_behind_a_wall_of_stuck_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """THE assertion for R2-09.

    250 permanently-blocked WAITING rows (dead-lettered parents, nothing can
    ever promote them) plus one healthy child whose parent completed and whose
    pause has lifted. Red before the fix: the sweep selected `WAITING LIMIT
    200` with no ORDER BY and no eligibility predicate, so the healthy child
    was simply never in the page — the 200 slots were spent on rows that were
    then discarded by the per-row `unmet_count` check, every pass, forever.
    """
    await _seed_blocked_wall(session_factory, 250)

    async with session_factory() as session:
        async with session.begin():
            done_parent = _job(
                status=JobStatus.COMPLETED,
                created_at=_EPOCH + timedelta(days=1),
            )
            healthy = _job(
                status=JobStatus.WAITING,
                created_at=_EPOCH + timedelta(days=1),
            )
            session.add_all([done_parent, healthy])
            session.add(
                JobDependency(
                    job_id=healthy.id, depends_on_job_id=done_parent.id
                )
            )
    healthy_id = healthy.id

    await _resume_unblocked_waiting_once(session_factory, _StubRedis())

    assert await _status_of(session_factory, healthy_id) == JobStatus.PENDING


async def test_stuck_rows_are_not_candidates_at_all(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The predicate excludes them rather than fetching-then-discarding.

    A pass over a pure wall of blocked rows must select nothing, which is
    what makes the LIMIT spend its budget only on promotable work. The
    returned cursor is None because the page was short.
    """
    await _seed_blocked_wall(session_factory, 250)

    cursor = await _resume_unblocked_waiting_once(session_factory, _StubRedis())

    assert cursor is None
    async with session_factory() as session:
        still_waiting = (
            await session.execute(
                select(Job).where(Job.status == JobStatus.WAITING)
            )
        ).scalars()
        assert len(list(still_waiting)) == 250


async def test_cursor_rotates_past_a_full_page_of_paused_children(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The second line of defence, for what the SQL predicate cannot see.

    A DAG pause lives in Redis, so paused children are promotable *in SQL* and
    do occupy the page. `_RESUME_SWEEP_LIMIT` paused children ahead of one
    unpaused child would re-create the starvation inside the eligible set. The
    ORDER BY plus rotating cursor means the second pass resumes past the first
    page instead of re-scanning it, so the unpaused child is reached.
    """
    paused_ids: list[uuid.UUID] = []
    async with session_factory() as session:
        async with session.begin():
            for i in range(_RESUME_SWEEP_LIMIT):
                parent = _job(
                    status=JobStatus.COMPLETED,
                    created_at=_EPOCH + timedelta(seconds=i),
                )
                child = _job(
                    status=JobStatus.WAITING,
                    created_at=_EPOCH + timedelta(seconds=i),
                )
                session.add_all([parent, child])
                session.add(
                    JobDependency(job_id=child.id, depends_on_job_id=parent.id)
                )
                paused_ids.append(child.id)

            late_parent = _job(
                status=JobStatus.COMPLETED, created_at=_EPOCH + timedelta(days=1)
            )
            late_child = _job(
                status=JobStatus.WAITING, created_at=_EPOCH + timedelta(days=1)
            )
            session.add_all([late_parent, late_child])
            session.add(
                JobDependency(
                    job_id=late_child.id, depends_on_job_id=late_parent.id
                )
            )
    late_id = late_child.id
    redis = _StubRedis(paused=set(paused_ids))

    cursor = await _resume_unblocked_waiting_once(session_factory, redis)
    # Full page of paused children: nothing promoted, cursor handed forward.
    assert cursor is not None
    assert await _status_of(session_factory, late_id) == JobStatus.WAITING

    await _resume_unblocked_waiting_once(session_factory, redis, cursor)

    assert await _status_of(session_factory, late_id) == JobStatus.PENDING


async def test_short_page_rotates_cursor_back_to_the_start(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A pass that reaches the tail resets, so the next one re-scans from the
    oldest row. Without the reset the cursor would march off the end and the
    sweep would go permanently blind."""
    async with session_factory() as session:
        async with session.begin():
            parent = _job(status=JobStatus.COMPLETED, created_at=_EPOCH)
            child = _job(status=JobStatus.WAITING, created_at=_EPOCH)
            session.add_all([parent, child])
            session.add(
                JobDependency(job_id=child.id, depends_on_job_id=parent.id)
            )

    assert await _resume_unblocked_waiting_once(session_factory, _StubRedis()) is None


async def test_promotion_still_emits_exactly_one_job_submitted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The outbox contract the sweep already owed (E1-04) survives the rewrite:
    one promotion, one `job.submitted`, and the CAS keeps a second pass from
    minting a duplicate."""
    async with session_factory() as session:
        async with session.begin():
            parent = _job(status=JobStatus.COMPLETED, created_at=_EPOCH)
            child = _job(status=JobStatus.WAITING, created_at=_EPOCH)
            session.add_all([parent, child])
            session.add(
                JobDependency(job_id=child.id, depends_on_job_id=parent.id)
            )
    child_id = child.id

    await _resume_unblocked_waiting_once(session_factory, _StubRedis())
    await _resume_unblocked_waiting_once(session_factory, _StubRedis())

    async with session_factory() as session:
        events = list(
            (await session.execute(select(OutboxEvent))).scalars()
        )
    submitted = [
        e for e in events if (e.payload or {}).get("job_id") == str(child_id)
    ]
    assert len(submitted) == 1
    assert submitted[0].payload["event"] == "job.submitted"


# --------------------------------------------------------------------------
# The cascade: stop the stuck set growing at the source.
# --------------------------------------------------------------------------


async def test_dead_letter_cascades_cancelled_to_waiting_children(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The behaviour the `CANCELLED` enum comment has advertised
    ("dependency parent failed") since the DAG landed, and which nothing
    implemented. Red before: the child stays WAITING forever.
    """
    async with session_factory() as session:
        async with session.begin():
            parent = _job(status=JobStatus.RUNNING, created_at=_EPOCH)
            child = _job(status=JobStatus.WAITING, created_at=_EPOCH)
            session.add_all([parent, child])
            session.add(
                JobDependency(job_id=child.id, depends_on_job_id=parent.id)
            )
    parent_id, child_id = parent.id, child.id

    async with session_factory() as session:
        async with session.begin():
            await JobRepository(session).update_status(
                parent_id, JobStatus.DEAD_LETTER
            )

    assert await _status_of(session_factory, child_id) == JobStatus.CANCELLED


async def test_cascade_reaches_grandchildren(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`unmet_count` treats a CANCELLED parent as unmet too, so stopping at
    the first level would just move the stuck set one generation down."""
    async with session_factory() as session:
        async with session.begin():
            root = _job(status=JobStatus.RUNNING, created_at=_EPOCH)
            mid = _job(status=JobStatus.WAITING, created_at=_EPOCH)
            leaf = _job(status=JobStatus.WAITING, created_at=_EPOCH)
            session.add_all([root, mid, leaf])
            session.add_all(
                [
                    JobDependency(job_id=mid.id, depends_on_job_id=root.id),
                    JobDependency(job_id=leaf.id, depends_on_job_id=mid.id),
                ]
            )
    root_id, mid_id, leaf_id = root.id, mid.id, leaf.id

    async with session_factory() as session:
        async with session.begin():
            await JobRepository(session).update_status(
                root_id, JobStatus.DEAD_LETTER
            )

    assert await _status_of(session_factory, mid_id) == JobStatus.CANCELLED
    assert await _status_of(session_factory, leaf_id) == JobStatus.CANCELLED


async def test_cascade_leaves_saga_steps_to_the_saga_coordinator(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Saga steps are cancelled by saga membership, not by the dependency DAG.
    Touching them here would double-cancel and race `SagaCoordinator`."""
    saga_id = uuid.uuid4()
    async with session_factory() as session:
        async with session.begin():
            session.add(
                Saga(
                    id=saga_id,
                    tenant_id=DEFAULT_TENANT_ID,
                    name="checkout",
                    status="running",
                )
            )
            parent = _job(status=JobStatus.RUNNING, created_at=_EPOCH)
            saga_child = _job(
                status=JobStatus.WAITING, created_at=_EPOCH, saga_id=saga_id
            )
            plain_child = _job(status=JobStatus.WAITING, created_at=_EPOCH)
            session.add_all([parent, saga_child, plain_child])
            session.add_all(
                [
                    JobDependency(
                        job_id=saga_child.id, depends_on_job_id=parent.id
                    ),
                    JobDependency(
                        job_id=plain_child.id, depends_on_job_id=parent.id
                    ),
                ]
            )
    parent_id, saga_child_id, plain_id = parent.id, saga_child.id, plain_child.id

    async with session_factory() as session:
        async with session.begin():
            await JobRepository(session).update_status(
                parent_id, JobStatus.DEAD_LETTER
            )

    assert await _status_of(session_factory, saga_child_id) == JobStatus.WAITING
    assert await _status_of(session_factory, plain_id) == JobStatus.CANCELLED


@pytest.mark.parametrize(
    "spared_status", [JobStatus.RUNNING, JobStatus.PENDING, JobStatus.COMPLETED]
)
async def test_cascade_only_touches_waiting_children(
    session_factory: async_sessionmaker[AsyncSession], spared_status: str
) -> None:
    """A child that is already in flight or finished is not ours to cancel —
    the WAITING predicate is a set-shaped CAS, not a filter of convenience."""
    async with session_factory() as session:
        async with session.begin():
            parent = _job(status=JobStatus.RUNNING, created_at=_EPOCH)
            child = _job(status=spared_status, created_at=_EPOCH)
            session.add_all([parent, child])
            session.add(
                JobDependency(job_id=child.id, depends_on_job_id=parent.id)
            )
    parent_id, child_id = parent.id, child.id

    async with session_factory() as session:
        async with session.begin():
            await JobRepository(session).update_status(
                parent_id, JobStatus.DEAD_LETTER
            )

    assert await _status_of(session_factory, child_id) == spared_status


async def test_failed_parent_does_not_cascade_because_retries_remain(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`FAILED` is absent from the cascade set for the same reason it is
    absent from `TERMINAL_JOB_STATUSES`: the retry cycle re-enters from it, so
    the parent may still complete. Cascading here would cancel the children of
    a job that is about to succeed."""
    async with session_factory() as session:
        async with session.begin():
            parent = _job(status=JobStatus.RUNNING, created_at=_EPOCH)
            child = _job(status=JobStatus.WAITING, created_at=_EPOCH)
            session.add_all([parent, child])
            session.add(
                JobDependency(job_id=child.id, depends_on_job_id=parent.id)
            )
    parent_id, child_id = parent.id, child.id

    async with session_factory() as session:
        async with session.begin():
            await JobRepository(session).update_status(
                parent_id, JobStatus.FAILED
            )

    assert await _status_of(session_factory, child_id) == JobStatus.WAITING


async def test_cascade_records_why_the_child_was_cancelled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """There is no `job.cancelled` topic, so the error message on the row is
    the only trace an operator has of why a child vanished from the DAG."""
    async with session_factory() as session:
        async with session.begin():
            parent = _job(status=JobStatus.RUNNING, created_at=_EPOCH)
            child = _job(status=JobStatus.WAITING, created_at=_EPOCH)
            session.add_all([parent, child])
            session.add(
                JobDependency(job_id=child.id, depends_on_job_id=parent.id)
            )
    parent_id, child_id = parent.id, child.id

    async with session_factory() as session:
        async with session.begin():
            await JobRepository(session).update_status(
                parent_id, JobStatus.DEAD_LETTER
            )

    async with session_factory() as session:
        msg = (
            await session.execute(
                select(Job.error_message).where(Job.id == child_id)
            )
        ).scalar_one()
    assert str(parent_id) in msg
    assert JobStatus.DEAD_LETTER in msg


async def test_cascaded_children_never_reappear_as_sweep_candidates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The two halves meet: the cascade drains the stuck set at the source,
    and what it leaves behind is not WAITING, so it cannot occupy the page."""
    async with session_factory() as session:
        async with session.begin():
            parent = _job(status=JobStatus.RUNNING, created_at=_EPOCH)
            child = _job(status=JobStatus.WAITING, created_at=_EPOCH)
            session.add_all([parent, child])
            session.add(
                JobDependency(job_id=child.id, depends_on_job_id=parent.id)
            )
    parent_id = parent.id

    async with session_factory() as session:
        async with session.begin():
            await JobRepository(session).update_status(
                parent_id, JobStatus.DEAD_LETTER
            )

    await _resume_unblocked_waiting_once(session_factory, _StubRedis())

    async with session_factory() as session:
        waiting = list(
            (
                await session.execute(
                    select(Job).where(Job.status == JobStatus.WAITING)
                )
            ).scalars()
        )
    assert waiting == []
