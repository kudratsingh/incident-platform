"""Resume sweep starvation and the dependency cascade (R2-09).

Real rows on a module-local SQLite engine: the fix is a `NOT EXISTS` subquery, an ORDER BY and a
keyset cursor, so a mocked session proves nothing, and committed rows never leak into the shared
`sqlite_engine`.
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

# Mixed hex like `DEFAULT_TENANT_ID`: all-digit hex round-trips through SQLite as a float.
_USER_ID = uuid.UUID("c4b5a697-8d9e-4f01-9a2b-3c4d5e6f7a8b")

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class _StubRedis:
    """Only the surface `find_blocking_pause` touches: `paused` holds the job ids whose DAG-pause
    flag is set, everything else reads as absent."""

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
        max_attempts=3,
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

    Inserted directly rather than by dead-lettering a parent, because that path now cascades them to
    CANCELLED, and direct inserts are how this state really accumulated (and how `create_stuck_dag`
    manufactures it). Oldest first, so an unordered `LIMIT 200` sees the wall."""
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
    """THE assertion for R2-09: 250 permanently-blocked WAITING rows plus one healthy child whose
    parent completed and whose pause has lifted. Red before the fix — `WAITING LIMIT 200` with no
    ORDER BY and no eligibility predicate spent all 200 slots on rows the per-row check then
    discarded, every pass, forever."""
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
    """The predicate excludes them rather than fetching-then-discarding, which is what makes the
    LIMIT spend its budget only on promotable work."""
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
    """The second line of defence, for what the SQL predicate cannot see: a DAG pause lives in
    Redis, so paused children are promotable in SQL and do occupy the page. The ORDER BY plus
    rotating cursor means the second pass resumes past the first page instead of re-scanning it."""
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
    """A pass that reaches the tail resets, or the cursor marches off the end and the sweep goes
    blind."""
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
    """The E1-04 outbox contract survives the rewrite: one promotion, one `job.submitted`, and the
    CAS stops a second pass minting a duplicate."""
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


# The cascade: stop the stuck set growing at the source.


async def test_dead_letter_cascades_cancelled_to_waiting_children(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The behaviour the `CANCELLED` enum comment has advertised since the DAG landed and nothing
    implemented: red before, the child stays WAITING forever."""
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
    """Saga steps belong to `SagaCoordinator`, not the dependency DAG."""
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
    """A child already in flight or finished is not ours to cancel: the WAITING predicate is a CAS.
    """
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
    """`FAILED` is absent from the cascade set for the same reason it is absent from
    `TERMINAL_JOB_STATUSES`: the retry cycle re-enters from it, so the parent may still complete."""
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
    """The row's error message says why a child vanished from the DAG. It used to be the only trace;
    `job.cancelled` exists now (WO-R2-113), but the row-level reason is what the admin UI and the
    timeline read."""
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


async def test_cascade_announces_every_child_it_cancels(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The cascade is the OTHER CANCELLED writer, and it is not `update_status` (WO-R2-113).

    #152 routed every terminal write through `update_status`, but the cascade writes CANCELLED with
    a set-based `UPDATE ... WHERE id IN (...)` for portability, so it never passes through the
    single writer — leaving exactly the jobs R2-09 cancels in bulk as the silent terminal state that
    order set out to remove. Every descendant gets its own event, at every depth, in the same
    transaction."""
    from app.config import get_settings

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

    topic = get_settings().kafka_topic_job_cancelled
    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.topic == topic)
                )
            )
            .scalars()
            .all()
        )
    announced = {r.payload["job_id"] for r in rows}
    assert announced == {str(mid_id), str(leaf_id)}
    assert all(r.payload["event"] == "job.cancelled" for r in rows)
    assert all(str(root_id) in r.payload["reason"] for r in rows)


async def test_cascade_stamps_completed_at_on_every_child(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """WO-R2-114 has the same second writer: stamping `completed_at` only inside `update_status`
    would make the stamp depend on which mechanism cancelled the row — worse than the uniform NULL
    it replaces, because it is invisible."""
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
        job = (
            await session.execute(select(Job).where(Job.id == child_id))
        ).scalar_one()
    assert job.status == JobStatus.CANCELLED
    assert job.completed_at is not None


async def test_cascade_cancellation_events_roll_back_with_the_status_write(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Same invariant the dead-letter side has (ADR 0001): the events are in the caller's
    transaction, so an abort leaves neither cancelled rows nor announcements of cancellations."""
    from app.config import get_settings

    async with session_factory() as session:
        async with session.begin():
            parent = _job(status=JobStatus.RUNNING, created_at=_EPOCH)
            child = _job(status=JobStatus.WAITING, created_at=_EPOCH)
            session.add_all([parent, child])
            session.add(
                JobDependency(job_id=child.id, depends_on_job_id=parent.id)
            )
    parent_id, child_id = parent.id, child.id

    with pytest.raises(RuntimeError):
        async with session_factory() as session:
            async with session.begin():
                await JobRepository(session).update_status(
                    parent_id, JobStatus.DEAD_LETTER
                )
                raise RuntimeError("caller blew up after the cascade")

    topic = get_settings().kafka_topic_job_cancelled
    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.topic == topic)
                )
            )
            .scalars()
            .all()
        )
        status = (
            await session.execute(select(Job.status).where(Job.id == child_id))
        ).scalar_one()
    assert rows == []
    assert status == JobStatus.WAITING


async def test_cascaded_children_never_reappear_as_sweep_candidates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The two halves meet: what the cascade leaves behind is not WAITING, so it cannot occupy the
    page."""
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


async def test_resume_sweep_publishes_the_shared_submitted_payload(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """WO-R2-116: the sweep built the `job.submitted` payload inline while every other re-publish
    path called `_job_submitted_payload`, so a backstop-dispatched job could stop being
    byte-identical to a normally dispatched one. Asserted as equality against the helper, so a field
    added there cannot pass while the sweep omits it."""
    from app.workers.dispatcher import _job_submitted_payload

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

    async with session_factory() as session:
        row = (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.topic == "job.submitted")
            )
        ).scalar_one()
        promoted = (
            await session.execute(select(Job).where(Job.id == child_id))
        ).scalar_one()

    assert row.payload == _job_submitted_payload(promoted)
