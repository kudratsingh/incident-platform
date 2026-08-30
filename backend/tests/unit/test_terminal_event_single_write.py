"""The invariant: a terminal job status and its lifecycle event are one write.

Real rows on a real (SQLite in-memory) engine rather than the mock-heavy
`test_dispatcher.py` style, for the same reason `test_stale_running_sweep.py`
is: the thing under test is what actually lands in two tables inside one
transaction, and a mocked session proves nothing about either.

Three writers used to break the invariant, all in the same way — they wrote a
terminal status and no event, so the job died (or completed) in Postgres while
every consumer downstream went on believing the old state:

  1. `JobDispatcherConsumer._force_dead_letter` — DEAD_LETTER, no `job.dlq`.
  2. `JobService.resolve_incident` — COMPLETED, no `job.completed`.
  3. the retry branch's unguarded `queue.push_delayed`, which is how jobs with
     retries left got fed to (1) in the first place.

The engine is module-local so committed rows never leak into the shared
session-scoped `sqlite_engine` other suites roll back against.
"""

import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from app.config import get_settings
from app.models.base import Base
from app.models.enums import JobStatus, JobType, SagaStatus, UserRole
from app.models.job import Job
from app.models.outbox import OutboxEvent
from app.models.tenant import DEFAULT_TENANT_ID, Tenant
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.job import JobRepository
from app.repositories.outbox import OutboxRepository
from app.schemas.job_events import DLQ_EVENT_KEYS
from app.services.job import JobService
from app.workers import dispatcher as dispatcher_mod
from app.workers import schema_registry
from app.workers.saga_coordinator import SagaCoordinator
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
_USER_ID = uuid.UUID("c4b3a291-8d7e-4f60-9a1b-2c3d4e5f6a7b")

_TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


@dataclass
class _StubConsumer:
    """Only the surface `_force_dead_letter` is allowed to touch."""

    session_factory: object
    in_flight_job_ids: set[str] = field(default_factory=set)


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
                email="terminal@test.example",
                hashed_password="x",
                role=UserRole.USER,
                is_active=True,
            )
        )
        await session.commit()

    yield factory
    await engine.dispose()


async def _insert_job(
    factory: async_sessionmaker[AsyncSession],
    *,
    status: str = JobStatus.RUNNING,
    job_type: str = JobType.CSV_UPLOAD,
    payload: dict[str, object] | None = None,
    retry_count: int = 0,
    trace_id: str | None = "trace-terminal",
    saga_id: uuid.UUID | None = None,
) -> uuid.UUID:
    job_id = uuid.uuid4()
    async with factory() as session:
        session.add(
            Job(
                id=job_id,
                tenant_id=DEFAULT_TENANT_ID,
                user_id=_USER_ID,
                type=job_type,
                status=status,
                priority=0,
                payload=payload if payload is not None else {"file": "x.csv"},
                retry_count=retry_count,
                max_retries=3,
                trace_id=trace_id,
                saga_id=saga_id,
            )
        )
        await session.commit()
    return job_id


async def _outbox_rows(
    factory: async_sessionmaker[AsyncSession], topic: str
) -> list[OutboxEvent]:
    async with factory() as session:
        result = await session.execute(
            select(OutboxEvent).where(OutboxEvent.topic == topic)
        )
        return list(result.scalars().all())


async def _job_status(
    factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> str:
    async with factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        return job.status


# --------------------------------------------------------------------------- #
# Finding 1 — _force_dead_letter wrote DEAD_LETTER and announced nothing        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_force_dead_letter_writes_status_and_dlq_event_together(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """RED before the fix: the job row flips to DEAD_LETTER and `outbox_events`
    stays empty, so the saga stays RUNNING forever, the read model keeps the id
    pinned in its old status set, LLM triage never sees the failure and the SSE
    stream never closes. GREEN after: both rows land in one transaction.
    """
    settings = get_settings()
    job_id = await _insert_job(
        session_factory,
        payload={"file": "x.csv", "__traceparent": {"traceparent": _TRACEPARENT}},
        retry_count=2,
    )
    consumer = _StubConsumer(session_factory=session_factory)

    await dispatcher_mod.JobDispatcherConsumer._force_dead_letter(
        consumer, str(job_id), "boom past guards"  # type: ignore[arg-type]
    )

    assert await _job_status(session_factory, job_id) == JobStatus.DEAD_LETTER

    rows = await _outbox_rows(session_factory, settings.kafka_topic_job_dlq)
    assert len(rows) == 1
    event = rows[0]
    assert event.tenant_id == DEFAULT_TENANT_ID
    assert event.key == f"{DEFAULT_TENANT_ID}:{_USER_ID}"

    payload = event.payload
    assert payload["dead_lettered"] is True
    assert payload["job_id"] == str(job_id)
    assert payload["error"] == "Dispatcher escape: boom past guards"
    # E1-14 triage context, owed by every DLQ producer.
    assert payload["retry_count"] == 2
    assert payload["max_retries"] == 3
    assert payload["trace_id"] == "trace-terminal"
    # The OTel carrier is tracing plumbing and must not ride along onto
    # `job_events`, which stores the event verbatim.
    assert payload["payload"] == {"file": "x.csv"}
    assert set(payload) == DLQ_EVENT_KEYS

    # And the relay will accept it: a derived payload that fails schema
    # validation would be marked permanently failed, losing the event in a
    # new way rather than the old one.
    schema_registry.validate(settings.kafka_topic_job_dlq, payload)


@pytest.mark.asyncio
async def test_force_dead_letter_leaves_an_already_settled_job_alone(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The safety net's guard checked only DEAD_LETTER, so a job that
    `_run_job` had already settled COMPLETED (the metrics emit after the commit
    can still raise) was overwritten to DEAD_LETTER. Harmless-ish while the
    path was silent; now that the write emits, it would broadcast the lie to
    every consumer. Any terminal state is left alone.
    """
    settings = get_settings()
    job_id = await _insert_job(session_factory, status=JobStatus.COMPLETED)
    consumer = _StubConsumer(session_factory=session_factory)

    await dispatcher_mod.JobDispatcherConsumer._force_dead_letter(
        consumer, str(job_id), "late boom"  # type: ignore[arg-type]
    )

    assert await _job_status(session_factory, job_id) == JobStatus.COMPLETED
    assert await _outbox_rows(session_factory, settings.kafka_topic_job_dlq) == []


# --------------------------------------------------------------------------- #
# Finding 3 — resolve_incident wrote COMPLETED and announced nothing            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolve_incident_emits_job_completed_and_settles_the_saga(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The two findings compounding, which is how this is actually hit: a saga
    step was force-dead-lettered by the dispatcher safety net, which emitted
    nothing, so the coordinator never learned and the saga stayed RUNNING. An
    operator then resolves the incident in the admin console.

    RED before the fix: `resolve_incident` flipped Postgres to COMPLETED and
    emitted nothing either, so the saga sat in RUNNING forever and
    `GET /admin/stats` counted the job as dead-lettered for the rest of time.

    GREEN after: the `job.completed` event rides on the same transaction, and
    feeding it to the saga coordinator — exactly what the relay would publish —
    settles the saga.
    """
    settings = get_settings()
    saga_id = uuid.uuid4()
    job_id = await _insert_job(
        session_factory, status=JobStatus.DEAD_LETTER, saga_id=saga_id
    )

    async with session_factory() as session:
        async with session.begin():
            service = JobService(
                job_repo=JobRepository(session),
                audit_repo=AuditRepository(session),
                outbox_repo=OutboxRepository(session),
                redis=AsyncMock(),
            )
            await service.resolve_incident(job_id, _USER_ID, DEFAULT_TENANT_ID)

    assert await _job_status(session_factory, job_id) == JobStatus.COMPLETED

    rows = await _outbox_rows(session_factory, settings.kafka_topic_job_completed)
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["event"] == "job.completed"
    assert payload["job_id"] == str(job_id)
    assert rows[0].key == f"{DEFAULT_TENANT_ID}:{_USER_ID}"
    schema_registry.validate(settings.kafka_topic_job_completed, payload)

    # The consumer half: this is the event the outbox relay publishes, and it
    # is what unsticks the saga. Repository access is mocked here — the point
    # is that the coordinator now receives an event at all.
    saga = MagicMock()
    saga.id = saga_id
    saga.status = SagaStatus.RUNNING
    saga.completed_at = None
    saga.tenant_id = DEFAULT_TENANT_ID

    settled_step = MagicMock()
    settled_step.id = job_id
    settled_step.saga_id = saga_id
    settled_step.status = JobStatus.COMPLETED
    # Not a `.compensate` type: this is an original step, so the coordinator
    # takes the completion branch rather than the compensation-settlement one.
    settled_step.type = JobType.CSV_UPLOAD.value

    saga_repo = AsyncMock()
    saga_repo.get_by_id.return_value = saga
    saga_repo.jobs.return_value = [settled_step]
    job_repo = AsyncMock()
    job_repo.get_by_id.return_value = settled_step

    coordinator_factory = MagicMock()
    begin_ctx = MagicMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)
    coord_session = AsyncMock()
    coord_session.__aenter__ = AsyncMock(return_value=coord_session)
    coord_session.__aexit__ = AsyncMock(return_value=False)
    coord_session.begin = MagicMock(return_value=begin_ctx)
    coordinator_factory.return_value = coord_session

    coordinator = SagaCoordinator(coordinator_factory)
    with patch(
        "app.workers.saga_coordinator.JobRepository", return_value=job_repo
    ), patch(
        "app.workers.saga_coordinator.SagaRepository", return_value=saga_repo
    ), patch(
        "app.workers.saga_coordinator.AuditRepository", return_value=AsyncMock()
    ):
        await coordinator.handle_message(
            topic=settings.kafka_topic_job_completed,
            key=rows[0].key,
            value=payload,
        )

    assert saga.status == SagaStatus.COMPLETED
    assert saga.completed_at is not None


# --------------------------------------------------------------------------- #
# The producer contract, asserted where the single producer now lives          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_update_status_emits_nothing_for_non_terminal_statuses(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A retry writes PENDING and its own `job.failed` "retrying" event. The
    repository must not add a terminal event on top of it."""
    job_id = await _insert_job(session_factory)

    async with session_factory() as session:
        async with session.begin():
            await JobRepository(session).update_status(
                job_id, JobStatus.PENDING, extra={"retry_count": 1}
            )

    async with session_factory() as session:
        rows = list((await session.execute(select(OutboxEvent))).scalars().all())
    assert rows == []


@pytest.mark.asyncio
async def test_update_status_cancelled_writes_no_event_because_no_topic_exists(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CANCELLED is terminal but has no `job.cancelled` topic to announce on,
    so the repository writes the status alone. Pinned as a deliberate hole
    rather than an oversight: the saga coordinator cancels steps of a saga it
    is already settling, so the saga side stays coherent — but the CQRS read
    model does keep those ids in their previous status set. Adding the topic
    means a schema-registry entry plus four consumers, tracked in the roadmap.
    """
    job_id = await _insert_job(session_factory)

    async with session_factory() as session:
        async with session.begin():
            await JobRepository(session).update_status(
                job_id, JobStatus.CANCELLED, extra={"error_message": "saga rollback"}
            )

    assert await _job_status(session_factory, job_id) == JobStatus.CANCELLED
    async with session_factory() as session:
        rows = list((await session.execute(select(OutboxEvent))).scalars().all())
    assert rows == []


@pytest.mark.asyncio
async def test_terminal_event_rolls_back_with_the_status_write(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """"Single transactional write" cuts both ways: if the transaction that
    dead-lettered the job rolls back, the announcement must roll back with it.
    An event for a death that never happened is as wrong as a death nobody
    hears about."""
    job_id = await _insert_job(session_factory)

    with pytest.raises(RuntimeError):
        async with session_factory() as session:
            async with session.begin():
                await JobRepository(session).update_status(
                    job_id,
                    JobStatus.DEAD_LETTER,
                    extra={"error_message": "boom"},
                )
                raise RuntimeError("caller blew up after the status write")

    assert await _job_status(session_factory, job_id) == JobStatus.RUNNING
    async with session_factory() as session:
        rows = list((await session.execute(select(OutboxEvent))).scalars().all())
    assert rows == []
