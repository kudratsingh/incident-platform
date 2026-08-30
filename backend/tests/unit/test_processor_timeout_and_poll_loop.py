"""Bounded processor execution and a poll loop the semaphore cannot stall
(WO-R2-07).

Two defects, one blast radius. `await processor(payload, _publish)` had no
deadline, and `handle_message` acquired the concurrency semaphore *inline in
the consumer's poll loop* — so MAX_CONCURRENT_JOBS long-running jobs stopped
the dispatcher group entirely, and the stale-RUNNING sweep deliberately
skipped exactly those ids (ADR 0019 §3). Nothing in the tree could recover it.

Real rows on a real (SQLite in-memory) engine for the terminal-state
assertions, mirroring `test_stale_running_sweep.py`: the claim under test is
that a timed-out job dead-letters *through* `JobRepository.update_status`, so
the row and its `job.dlq` outbox event land in one transaction (ADR 0001
addendum / the terminal single-writer). A mocked session proves neither.

The engine is module-local so committed rows never leak into the shared
session-scoped `sqlite_engine` other suites roll back against.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from app.config import get_settings
from app.models.base import Base
from app.models.enums import JobStatus, JobType, UserRole
from app.models.job import Job
from app.models.outbox import OutboxEvent
from app.models.tenant import DEFAULT_TENANT_ID, Tenant
from app.models.user import User
from app.workers import dispatcher as dispatcher_mod
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
_USER_ID = uuid.UUID("c4b5a697-8d9e-4a0b-9c1d-2e3f4a5b6c7d")

# Short enough to keep the suite fast, long enough that a healthy processor
# in these tests finishes well inside it.
_TEST_TIMEOUT_SECONDS = 0.25


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


async def _seed_pending_job(
    factory: async_sessionmaker[AsyncSession],
    *,
    job_type: str = JobType.CSV_UPLOAD,
    retry_count: int = 0,
    max_retries: int = 3,
) -> uuid.UUID:
    job_id = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            session.add(
                Job(
                    id=job_id,
                    tenant_id=DEFAULT_TENANT_ID,
                    user_id=_USER_ID,
                    type=job_type,
                    status=JobStatus.PENDING,
                    payload={"row_count": 10, "chunk_size": 1},
                    retry_count=retry_count,
                    max_retries=max_retries,
                    trace_id="trace-abc",
                )
            )
    return job_id


async def _job(factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> Job:
    async with factory() as session:
        return (
            await session.execute(select(Job).where(Job.id == job_id))
        ).scalar_one()


async def _outbox(factory: async_sessionmaker[AsyncSession]) -> list[OutboxEvent]:
    async with factory() as session:
        return list((await session.execute(select(OutboxEvent))).scalars().all())


@pytest.fixture
def bounded_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the execution deadline and stub the I/O `_run_job` does around
    the processor call (progress publish, pause probe, Redis)."""
    monkeypatch.setattr(
        get_settings(), "job_execution_timeout_seconds", _TEST_TIMEOUT_SECONDS
    )
    monkeypatch.setattr(
        dispatcher_mod.kafka_producer, "publish_job_progress", AsyncMock()
    )
    monkeypatch.setattr(
        dispatcher_mod, "find_blocking_pause", AsyncMock(return_value=None)
    )


def _hanging_processor(entered: asyncio.Event) -> Any:
    async def _processor(_payload: dict[str, Any], _publish: Any) -> dict[str, Any]:
        entered.set()
        await asyncio.sleep(3600)
        return {"unreachable": True}

    return _processor


# --------------------------------------------------------------------------- #
# 1. The processor deadline                                                     #
# --------------------------------------------------------------------------- #


async def test_hung_processor_dead_letters_with_a_distinct_timeout_reason(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    bounded_execution: None,
) -> None:
    """THE assertion for the unbounded-execution half of the finding.

    Before the fix `_run_job` awaited the processor forever, so this call
    never returned and the outer `wait_for` was the only thing that ended
    the test.
    """
    job_id = await _seed_pending_job(session_factory)
    entered = asyncio.Event()
    monkeypatch.setitem(
        dispatcher_mod._PROCESSORS, JobType.CSV_UPLOAD, _hanging_processor(entered)
    )

    # Generous relative to the 0.25s deadline: this bound is here to fail the
    # test on a hang, not to measure the deadline.
    await asyncio.wait_for(
        dispatcher_mod._run_job(str(job_id), session_factory, AsyncMock()),
        timeout=10,
    )

    assert entered.is_set(), "the processor never ran — wrong failure"
    job = await _job(session_factory, job_id)
    assert job.status == JobStatus.DEAD_LETTER
    # Distinct from an ordinary processor exception: an operator reading the
    # DLQ tab must be able to tell "this job is too big" from "this job threw".
    assert "timed out" in (job.error_message or "")
    assert str(_TEST_TIMEOUT_SECONDS) in (job.error_message or "")
    assert job.dead_lettered_by == "execution_timeout"
    assert job.completed_at is not None


async def test_timeout_dead_letter_emits_its_terminal_event_in_the_same_write(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    bounded_execution: None,
) -> None:
    """The timeout path must go through the terminal single-writer, not write
    a status by hand — otherwise the job dies in Postgres and no consumer ever
    hears (saga stranded RUNNING, id pinned in the read model, triage never
    runs, SSE never closes)."""
    job_id = await _seed_pending_job(session_factory)
    entered = asyncio.Event()
    monkeypatch.setitem(
        dispatcher_mod._PROCESSORS, JobType.CSV_UPLOAD, _hanging_processor(entered)
    )

    await asyncio.wait_for(
        dispatcher_mod._run_job(str(job_id), session_factory, AsyncMock()),
        timeout=10,
    )

    dlq = [e for e in await _outbox(session_factory) if e.topic.endswith("job.dlq")]
    assert len(dlq) == 1, "exactly one job.dlq event for one dead-lettered job"
    payload = dlq[0].payload
    assert payload["job_id"] == str(job_id)
    assert payload["dead_lettered"] is True
    assert "timed out" in payload["error"]


async def test_timeout_does_not_burn_the_retry_budget(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    bounded_execution: None,
) -> None:
    """A deadline breach is deterministic in the payload — retrying it just
    spends another full deadline holding another slot. It dead-letters on the
    first breach, with `retry_count` left where it was so the DLQ tab and
    triage still see the real attempt history."""
    job_id = await _seed_pending_job(session_factory, retry_count=1)
    entered = asyncio.Event()
    monkeypatch.setitem(
        dispatcher_mod._PROCESSORS, JobType.CSV_UPLOAD, _hanging_processor(entered)
    )

    await asyncio.wait_for(
        dispatcher_mod._run_job(str(job_id), session_factory, AsyncMock()),
        timeout=10,
    )

    job = await _job(session_factory, job_id)
    assert job.status == JobStatus.DEAD_LETTER
    assert job.retry_count == 1


async def test_a_processors_own_timeout_error_still_takes_the_retry_path(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    bounded_execution: None,
) -> None:
    """`TimeoutError` from inside the processor (an HTTP client giving up) is
    an ordinary transient failure and must keep its retries — only the
    dispatcher's own deadline dead-letters. Catching bare `TimeoutError`
    around the call would have conflated the two."""
    job_id = await _seed_pending_job(session_factory)

    async def _client_timeout(_payload: dict[str, Any], _publish: Any) -> dict[str, Any]:
        raise TimeoutError("upstream API did not respond")

    monkeypatch.setitem(
        dispatcher_mod._PROCESSORS, JobType.CSV_UPLOAD, _client_timeout
    )
    monkeypatch.setattr(dispatcher_mod.queue, "push_delayed", AsyncMock())

    await dispatcher_mod._run_job(str(job_id), session_factory, AsyncMock())

    job = await _job(session_factory, job_id)
    assert job.status == JobStatus.PENDING, "should be scheduled for retry"
    assert job.retry_count == 1
    assert job.dead_lettered_by is None


async def test_a_processor_inside_the_deadline_still_completes(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    bounded_execution: None,
) -> None:
    """The deadline must not change the happy path."""
    job_id = await _seed_pending_job(session_factory)

    async def _quick(_payload: dict[str, Any], _publish: Any) -> dict[str, Any]:
        return {"rows": 10}

    monkeypatch.setitem(dispatcher_mod._PROCESSORS, JobType.CSV_UPLOAD, _quick)

    await dispatcher_mod._run_job(str(job_id), session_factory, AsyncMock())

    job = await _job(session_factory, job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.result == {"rows": 10}


# --------------------------------------------------------------------------- #
# 2. The poll loop                                                              #
# --------------------------------------------------------------------------- #


async def test_saturated_dispatcher_does_not_block_the_poll_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE load-bearing assertion.

    With every slot held, `handle_message` must still return promptly. It
    used to `await self.semaphore.acquire()` inline, and `handle_message`
    runs on the consumer's poll loop — so a saturated worker stopped calling
    `getmany()`, and once `fetcher_idle_time` passed `max_poll_interval_ms`
    the broker evicted the consumer from the group with nothing to restart
    it.

    Before the fix the second `handle_message` never returned and this raised
    `TimeoutError`.
    """
    consumer = dispatcher_mod.JobDispatcherConsumer(
        MagicMock(), MagicMock(), max_concurrent=1
    )
    release = asyncio.Event()
    started: list[str] = []

    async def _fake_run_job(jid: str, _factory: object, _redis: object) -> None:
        started.append(jid)
        await release.wait()

    monkeypatch.setattr(dispatcher_mod, "_run_job", _fake_run_job)
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        await consumer.handle_message("job.submitted", "k", {"job_id": first})
        await asyncio.sleep(0)  # let the first task take the only slot
        assert started == [first]

        # The slot is gone. The poll loop must keep moving anyway.
        await asyncio.wait_for(
            consumer.handle_message("job.submitted", "k", {"job_id": second}),
            timeout=2,
        )

        # Dispatched and accounted for, even though it cannot run yet: the
        # sweep must not see it as an orphan either.
        assert second in consumer.in_flight_job_ids
        assert len(consumer.in_flight) == 2
        assert started == [first], "the concurrency cap still holds"
    finally:
        release.set()
        await asyncio.gather(*consumer.in_flight, return_exceptions=True)


async def test_queued_job_runs_once_the_hung_one_hits_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two halves together: the poll loop keeps accepting while a job is
    hung, and the hung job's deadline is what eventually frees the slot for
    the message that was accepted behind it."""
    consumer = dispatcher_mod.JobDispatcherConsumer(
        MagicMock(), MagicMock(), max_concurrent=1
    )
    started: list[str] = []
    first, second = str(uuid.uuid4()), str(uuid.uuid4())

    async def _fake_run_job(jid: str, _factory: object, _redis: object) -> None:
        started.append(jid)
        if jid == first:
            # Stands in for a processor the deadline cancels.
            await asyncio.sleep(0.2)

    monkeypatch.setattr(dispatcher_mod, "_run_job", _fake_run_job)

    await consumer.handle_message("job.submitted", "k", {"job_id": first})
    await asyncio.wait_for(
        consumer.handle_message("job.submitted", "k", {"job_id": second}), timeout=2
    )
    await asyncio.wait_for(
        asyncio.gather(*consumer.in_flight, return_exceptions=True), timeout=5
    )

    assert started == [first, second]
    assert consumer.in_flight_job_ids == set()
    assert consumer.semaphore.locked() is False


async def test_dispatch_backlog_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not blocking the poll loop must not mean spawning tasks without limit.

    Past the backlog cap `handle_message` raises, which is the base
    consumer's existing backpressure primitive: the offset is not committed
    and the partition seeks back for redelivery. The poll loop keeps polling
    and heartbeating throughout — that is the whole difference from the old
    inline `acquire()`.
    """
    consumer = dispatcher_mod.JobDispatcherConsumer(
        MagicMock(), MagicMock(), max_concurrent=1
    )
    release = asyncio.Event()

    async def _fake_run_job(_jid: str, _factory: object, _redis: object) -> None:
        await release.wait()

    monkeypatch.setattr(dispatcher_mod, "_run_job", _fake_run_job)
    monkeypatch.setattr(dispatcher_mod, "_MAX_DISPATCH_BACKLOG", 3)
    try:
        for _ in range(3):
            await consumer.handle_message(
                "job.submitted", "k", {"job_id": str(uuid.uuid4())}
            )
        with pytest.raises(dispatcher_mod.DispatchBacklogFull):
            await consumer.handle_message(
                "job.submitted", "k", {"job_id": str(uuid.uuid4())}
            )
        # The rejected id must not be left claimed — the sweep would then
        # skip a row nobody is executing.
        assert len(consumer.in_flight_job_ids) == 3
    finally:
        release.set()
        await asyncio.gather(*consumer.in_flight, return_exceptions=True)


# --------------------------------------------------------------------------- #
# 3. The sweep's in-flight exclusion is no longer permanent                     #
# --------------------------------------------------------------------------- #


async def _seed_running_job(
    factory: async_sessionmaker[AsyncSession], *, age_seconds: float
) -> uuid.UUID:
    job_id = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            session.add(
                Job(
                    id=job_id,
                    tenant_id=DEFAULT_TENANT_ID,
                    user_id=_USER_ID,
                    type=JobType.CSV_UPLOAD,
                    status=JobStatus.RUNNING,
                    payload={"row_count": 10},
                    retry_count=0,
                    max_retries=3,
                    trace_id="trace-abc",
                    started_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
                )
            )
    return job_id


class _StubDispatcher:
    def __init__(self, in_flight_job_ids: set[str]) -> None:
        self.in_flight_job_ids = in_flight_job_ids


async def test_in_flight_exclusion_lapses_for_a_genuinely_stuck_local_job(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ADR 0019 skipped this process's in-flight ids unconditionally, so a
    job hung past its own deadline was the one thing no sweep could reclaim.

    Now that execution is bounded, an in-flight id still RUNNING long past
    the deadline is stuck, not slow — the exclusion lapses and the sweep
    recovers it.
    """
    threshold = 900
    stuck_id = await _seed_running_job(
        session_factory,
        age_seconds=threshold + dispatcher_mod._IN_FLIGHT_EXCLUSION_GRACE_SECONDS + 60,
    )
    dispatcher = _StubDispatcher({str(stuck_id)})

    recovered = await dispatcher_mod._sweep_stale_running_once(
        session_factory, dispatcher, threshold
    )

    assert recovered == 1
    assert (await _job(session_factory, stuck_id)).status == JobStatus.DEAD_LETTER


async def test_in_flight_exclusion_still_covers_a_job_inside_the_grace(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The grace is what keeps the sweep from reaping a job whose deadline
    just fired and whose dead-letter write is still in flight — that race
    would fan out a spurious `job.dlq` and then be overwritten."""
    threshold = 900
    live_id = await _seed_running_job(session_factory, age_seconds=threshold + 30)
    dispatcher = _StubDispatcher({str(live_id)})

    recovered = await dispatcher_mod._sweep_stale_running_once(
        session_factory, dispatcher, threshold
    )

    assert recovered == 0
    assert (await _job(session_factory, live_id)).status == JobStatus.RUNNING
