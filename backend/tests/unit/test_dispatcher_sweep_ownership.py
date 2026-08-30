"""Ownership and de-duplication semantics for the dispatcher sweeps (WO-R2-28).

Both sweeps had the same defect in different clothes: acting on rows they did
not own and could not tell they had already acted on.

  * the stale-PENDING backstop re-published a job every 60s for as long as the
    dispatcher was behind, because re-publishing changed nothing about the row
    and the row therefore stayed inside the backstop's own predicate;
  * the stale-RUNNING sweep excluded only the *local* process's in-flight ids,
    a set that lives in one replica's memory, so a second replica read another
    replica's live work as crash orphans and dead-lettered it.

Real rows on a real (SQLite in-memory) engine, for the same reason
`test_stale_running_sweep.py` uses one: both fixes are WHERE clauses and a
compare-and-set, and a mocked session proves nothing about either. The engine
is module-local so committed rows never leak into the shared session-scoped
`sqlite_engine` other suites roll back against.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from app.models.audit import AuditLog
from app.models.base import Base
from app.models.enums import JobStatus, JobType, UserRole
from app.models.job import Job
from app.models.outbox import OutboxEvent
from app.models.tenant import DEFAULT_TENANT_ID, Tenant
from app.models.user import User
from app.workers import dispatcher as dispatcher_mod
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

THRESHOLD_SECONDS = 900
# Mixed hex on purpose, same reason `DEFAULT_TENANT_ID` is: an all-digit UUID
# hex round-trips through SQLite's NUMERIC affinity as a float and blows up
# the UUID result processor.
_USER_ID = uuid.UUID("c4d5e6f7-a8b9-4c1d-8e2f-3a4b5c6d7e8f")


@dataclass
class _StubDispatcher:
    """Only the surface the sweep and the renewal loop are allowed to touch."""

    in_flight_job_ids: set[str] = field(default_factory=set)


class _NoTimerRedis:
    """Redis stub with nothing parked on `jobs:delayed`.

    A ZSCORE hit means the promotion loop still owns the job and the backstop
    must leave it alone; every job in this module is deliberately orphaned, so
    the answer is always None.
    """

    async def zscore(self, key: str, member: str) -> float | None:
        return None


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
                    email="sweeps@example.com",
                    hashed_password="not-a-real-hash",
                    role=UserRole.USER,
                    is_active=True,
                )
            )
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_job(
    factory: async_sessionmaker[AsyncSession],
    *,
    status: str,
    age_seconds: float,
    heartbeat_age_seconds: float | None = None,
    requeued_age_seconds: float | None = None,
) -> uuid.UUID:
    """Seed one job aged `age_seconds`.

    For PENDING the age is written to `updated_at` (the backstop's staleness
    signal); for RUNNING it is written to `started_at` (the sweep's).
    """
    now = datetime.now(UTC)
    job_id = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            session.add(
                Job(
                    id=job_id,
                    tenant_id=DEFAULT_TENANT_ID,
                    user_id=_USER_ID,
                    type=JobType.CSV_UPLOAD,
                    status=status,
                    payload={"rows": 10},
                    max_retries=3,
                    trace_id="trace-sweep",
                    created_at=now - timedelta(seconds=age_seconds),
                    updated_at=now - timedelta(seconds=age_seconds),
                    started_at=(
                        now - timedelta(seconds=age_seconds)
                        if status == JobStatus.RUNNING
                        else None
                    ),
                    heartbeat_at=(
                        None
                        if heartbeat_age_seconds is None
                        else now - timedelta(seconds=heartbeat_age_seconds)
                    ),
                    requeued_at=(
                        None
                        if requeued_age_seconds is None
                        else now - timedelta(seconds=requeued_age_seconds)
                    ),
                )
            )
    return job_id


async def _job(
    factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> Job:
    async with factory() as session:
        return (
            await session.execute(select(Job).where(Job.id == job_id))
        ).scalar_one()


async def _submitted_events(
    factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> list[OutboxEvent]:
    async with factory() as session:
        rows = (await session.execute(select(OutboxEvent))).scalars().all()
    return [r for r in rows if r.payload.get("job_id") == str(job_id)]


# ---------------------------------------------------------------------------
# Stale-PENDING backstop: de-duplication
# ---------------------------------------------------------------------------


async def test_stale_pending_backstop_publishes_once_per_cutoff_window(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """THE assertion for finding 1.

    The backstop runs every 60s and its staleness cutoff is 300s. Before the
    fix a re-publish touched nothing on the row, so the row stayed inside the
    predicate it had just matched and every single pass re-published it —
    turning a dispatcher that was merely behind into unbounded duplicate
    `job.submitted` traffic, for exactly as long as the lag lasted.

    Two passes in a row stand in for that: today's code emits two events,
    which is the shape of "every 60s, forever".
    """
    job_id = await _seed_job(
        session_factory,
        status=JobStatus.PENDING,
        age_seconds=dispatcher_mod._STALE_PENDING_AGE_SECONDS * 2,
    )
    redis = _NoTimerRedis()

    await dispatcher_mod._requeue_stale_pending_once(session_factory, redis)
    await dispatcher_mod._requeue_stale_pending_once(session_factory, redis)

    events = await _submitted_events(session_factory, job_id)
    assert len(events) == 1, (
        "the backstop re-published the same job on consecutive passes — a "
        "dispatcher that is behind mints one duplicate job.submitted per "
        "sweep interval for as long as it stays behind"
    )
    assert events[0].payload["event"] == "job.submitted"

    # And the row now carries the marker that took it out of the predicate.
    assert (await _job(session_factory, job_id)).requeued_at is not None


async def test_stale_pending_backstop_publishes_again_once_the_window_lapses(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """De-duplication must not become a one-shot.

    The backstop exists because a `job.submitted` can be lost outright (a
    crash between the Lua pop and the outbox commit). If the re-published
    event is lost too, the next window has to try again — suppressing a job
    forever after one attempt would trade unbounded duplicates for a
    permanently stranded job, which is the worse of the two.
    """
    job_id = await _seed_job(
        session_factory,
        status=JobStatus.PENDING,
        age_seconds=dispatcher_mod._STALE_PENDING_AGE_SECONDS * 2,
        # Re-published, but a full cutoff window ago.
        requeued_age_seconds=dispatcher_mod._STALE_PENDING_AGE_SECONDS * 2,
    )

    await dispatcher_mod._requeue_stale_pending_once(
        session_factory, _NoTimerRedis()
    )

    assert len(await _submitted_events(session_factory, job_id)) == 1


async def test_stale_pending_backstop_does_not_reset_the_visible_age(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The marker is its own column, not a bump of `updated_at`.

    `updated_at` is the staleness signal itself and is rendered in the DLQ
    list and the trace views. Re-publishing is the sweep noticing a problem,
    not the job making progress: if it moved `updated_at`, a job stuck for an
    hour would read as five minutes old to whoever is looking at it, and the
    backstop's own write would be indistinguishable from a real retry.
    """
    job_id = await _seed_job(
        session_factory,
        status=JobStatus.PENDING,
        age_seconds=dispatcher_mod._STALE_PENDING_AGE_SECONDS * 4,
    )
    before = (await _job(session_factory, job_id)).updated_at

    await dispatcher_mod._requeue_stale_pending_once(
        session_factory, _NoTimerRedis()
    )

    after = await _job(session_factory, job_id)
    assert after.updated_at == before
    assert after.requeued_at is not None


# ---------------------------------------------------------------------------
# Stale-RUNNING sweep: cross-replica ownership
# ---------------------------------------------------------------------------


async def test_replica_a_cannot_dead_letter_a_job_replica_b_is_executing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """THE assertion for finding 2.

    Two replicas, one database. Replica B is executing the job and renewing
    its lease; replica A sweeps and has an empty in-flight set, because that
    set only ever contains its own work. Before the fix A had no other signal
    to consult, so it dead-lettered a job that was running fine — and fired a
    real `job.dlq`, which fans out to triage, the saga coordinator and the
    event log.
    """
    job_id = await _seed_job(
        session_factory,
        status=JobStatus.RUNNING,
        age_seconds=THRESHOLD_SECONDS * 2,
        heartbeat_age_seconds=1.0,  # replica B checked in a second ago
    )
    replica_a = _StubDispatcher(in_flight_job_ids=set())

    recovered = await dispatcher_mod._sweep_stale_running_once(
        session_factory, replica_a, THRESHOLD_SECONDS
    )

    assert recovered == 0
    assert (await _job(session_factory, job_id)).status == JobStatus.RUNNING
    async with session_factory() as session:
        assert (await session.execute(select(OutboxEvent))).scalars().all() == []
        assert (await session.execute(select(AuditLog))).scalars().all() == []


async def test_a_job_whose_lease_lapsed_is_still_reclaimed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The lease must not become a way to never recover anything.

    A worker killed mid-job stops checking in, so its lease goes stale and the
    row becomes reclaimable by any replica — which is the whole point of the
    sweep (ADR 0019). A lease older than the TTL is indistinguishable from no
    lease at all.
    """
    job_id = await _seed_job(
        session_factory,
        status=JobStatus.RUNNING,
        age_seconds=THRESHOLD_SECONDS * 2,
        heartbeat_age_seconds=dispatcher_mod._RUNNING_LEASE_TTL_SECONDS + 60,
    )

    recovered = await dispatcher_mod._sweep_stale_running_once(
        session_factory, _StubDispatcher(), THRESHOLD_SECONDS
    )

    assert recovered == 1
    assert (await _job(session_factory, job_id)).status == JobStatus.DEAD_LETTER


class _MutateBeforeSession:
    """Session factory that runs `mutate` just before the Nth session opens.

    The sweep scans in one transaction and settles each row in another, so the
    interesting races all happen in the gap between them. Wrapping the factory
    is how this suite gets into that gap without reaching into the sweep.
    """

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        mutate: Callable[[], Awaitable[None]],
        *,
        before_call: int,
    ) -> None:
        self._factory = factory
        self._mutate = mutate
        self._before_call = before_call
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        if self.calls == self._before_call:
            return _MutatingSession(self._factory, self._mutate)
        return self._factory()


class _MutatingSession:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        mutate: Callable[[], Awaitable[None]],
    ) -> None:
        self._factory = factory
        self._mutate = mutate
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> AsyncSession:
        await self._mutate()
        self._session = self._factory()
        return await self._session.__aenter__()

    async def __aexit__(self, *exc: Any) -> Any:
        assert self._session is not None
        return await self._session.__aexit__(*exc)


async def test_recovery_write_is_refused_when_the_lease_is_renewed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The compare-and-set, on the lease.

    The scan saw a stale lease; by the time the recovery write runs, the
    replica executing the job has checked in. The row is alive after all and
    the write must not land. A re-read cannot close this gap — the check and
    the write have to be the same statement.
    """
    job_id = await _seed_job(
        session_factory,
        status=JobStatus.RUNNING,
        age_seconds=THRESHOLD_SECONDS * 2,
        heartbeat_age_seconds=dispatcher_mod._RUNNING_LEASE_TTL_SECONDS + 60,
    )

    async def _renew() -> None:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(Job)
                    .where(Job.id == job_id)
                    .values(heartbeat_at=datetime.now(UTC))
                )

    factory = _MutateBeforeSession(session_factory, _renew, before_call=2)

    recovered = await dispatcher_mod._sweep_stale_running_once(
        factory, _StubDispatcher(), THRESHOLD_SECONDS  # type: ignore[arg-type]
    )

    assert recovered == 0
    assert (await _job(session_factory, job_id)).status == JobStatus.RUNNING
    async with session_factory() as session:
        assert (await session.execute(select(OutboxEvent))).scalars().all() == []


async def test_recovery_write_is_refused_when_the_job_was_re_claimed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The compare-and-set, on `started_at`.

    The other thing that can happen in the gap: the job settles and is
    replayed, so the row is RUNNING again — but it is a *new* attempt, and
    dead-lettering it would kill work that has only just started. `started_at`
    is what distinguishes the attempt the scan saw from the one in front of
    the write.
    """
    job_id = await _seed_job(
        session_factory,
        status=JobStatus.RUNNING,
        age_seconds=THRESHOLD_SECONDS * 2,
    )

    async def _reclaim() -> None:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(Job)
                    .where(Job.id == job_id)
                    .values(started_at=datetime.now(UTC))
                )

    factory = _MutateBeforeSession(session_factory, _reclaim, before_call=2)

    recovered = await dispatcher_mod._sweep_stale_running_once(
        factory, _StubDispatcher(), THRESHOLD_SECONDS  # type: ignore[arg-type]
    )

    assert recovered == 0
    assert (await _job(session_factory, job_id)).status == JobStatus.RUNNING


# ---------------------------------------------------------------------------
# Lease renewal
# ---------------------------------------------------------------------------


async def test_lease_renewal_vouches_for_this_workers_running_jobs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The write side of the lease: what makes the sweep above skip a job."""
    mine = await _seed_job(
        session_factory, status=JobStatus.RUNNING, age_seconds=60
    )
    theirs = await _seed_job(
        session_factory, status=JobStatus.RUNNING, age_seconds=60
    )

    renewed = await dispatcher_mod._renew_running_leases_once(
        session_factory,
        _StubDispatcher(in_flight_job_ids={str(mine)}),
        THRESHOLD_SECONDS,
    )

    assert renewed == 1
    assert (await _job(session_factory, mine)).heartbeat_at is not None
    assert (await _job(session_factory, theirs)).heartbeat_at is None


async def test_lease_renewal_stops_once_the_job_is_past_its_deadline(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A hung worker must not defend its own stuck job forever.

    The renewal loop keeps running even when the job it is vouching for has
    wedged, so an unconditional check-in would re-create — through the lease —
    the single unreclaimable state WO-R2-07 removed. Past the threshold plus
    the in-flight grace the check-ins stop and the lease lapses on its own.
    """
    stuck = await _seed_job(
        session_factory,
        status=JobStatus.RUNNING,
        age_seconds=(
            THRESHOLD_SECONDS
            + dispatcher_mod._IN_FLIGHT_EXCLUSION_GRACE_SECONDS
            + 60
        ),
    )

    renewed = await dispatcher_mod._renew_running_leases_once(
        session_factory,
        _StubDispatcher(in_flight_job_ids={str(stuck)}),
        THRESHOLD_SECONDS,
    )

    assert renewed == 0
    assert (await _job(session_factory, stuck)).heartbeat_at is None


async def test_lease_renewal_does_not_reset_the_visible_age(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A check-in is not progress.

    This statement runs every renewal interval for every running job. If it
    moved `updated_at` it would churn a column the DLQ list and trace views
    render, and a job wedged for an hour would read as freshly touched.
    """
    job_id = await _seed_job(
        session_factory, status=JobStatus.RUNNING, age_seconds=120
    )
    before = (await _job(session_factory, job_id)).updated_at

    await dispatcher_mod._renew_running_leases_once(
        session_factory,
        _StubDispatcher(in_flight_job_ids={str(job_id)}),
        THRESHOLD_SECONDS,
    )

    assert (await _job(session_factory, job_id)).updated_at == before


async def test_lease_renewal_ignores_jobs_that_are_no_longer_running(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The id set is dropped in `_run_and_release`'s finally block, so a job
    can settle between the snapshot and the write. The status predicate is
    what keeps a check-in off a terminal row."""
    job_id = await _seed_job(
        session_factory, status=JobStatus.COMPLETED, age_seconds=60
    )

    renewed = await dispatcher_mod._renew_running_leases_once(
        session_factory,
        _StubDispatcher(in_flight_job_ids={str(job_id)}),
        THRESHOLD_SECONDS,
    )

    assert renewed == 0


async def test_lease_renewal_tolerates_a_malformed_in_flight_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`handle_message` puts whatever the message carried into the set, so a
    non-UUID id can reach the renewal pass. It has no row and therefore no
    lease; it must be skipped rather than crash the loop."""
    renewed = await dispatcher_mod._renew_running_leases_once(
        session_factory,
        _StubDispatcher(in_flight_job_ids={"not-a-uuid"}),
        THRESHOLD_SECONDS,
    )

    assert renewed == 0


@pytest.mark.parametrize("loop_name", ["_renew_running_leases_loop"])
async def test_lease_renewal_loop_is_registered_in_worker_loop(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
    loop_name: str,
) -> None:
    """A loop nobody starts is a lease nobody renews.

    The sweep's cross-replica exclusion is only as good as the check-ins that
    feed it: if this loop is ever dropped from `worker_loop`'s task list, every
    lease in the fleet goes stale and the sweep silently reverts to the
    local-only behaviour this order exists to fix. Asserted by running the real
    `worker_loop` with its loops replaced by recorders.
    """
    started: set[str] = set()

    def _recorder(name: str) -> Any:
        async def _loop(*_args: Any, **_kwargs: Any) -> None:
            started.add(name)
            await asyncio.Event().wait()

        return _loop

    for name in (
        "_promote_delayed_loop",
        "_promote_dlq_replay_loop",
        "_resume_unblocked_waiting_loop",
        "_requeue_stale_pending_loop",
        "_outbox_relay_loop",
        "_metrics_loop",
        "_digest_loop",
        "_idempotency_reaper_loop",
        "_stale_running_sweep_loop",
        "_renew_running_leases_loop",
    ):
        monkeypatch.setattr(dispatcher_mod, name, _recorder(name))
    monkeypatch.setattr(dispatcher_mod, "_supervise_consumer", _recorder("consumers"))

    task = asyncio.create_task(dispatcher_mod.worker_loop(session_factory, None))
    for _ in range(20):
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert loop_name in started, (
        f"{loop_name} is not in worker_loop's task list — nothing would start it"
    )
