"""Stale-RUNNING crash-recovery sweep (E1-17, ADR 0019).

Real rows on a real (SQLite in-memory) engine rather than the mock-heavy
`test_dispatcher.py` style: the whole point of the fix is a SQL WHERE clause
comparing an aware cutoff against a `TIMESTAMP WITH TIME ZONE` column that
SQLite hands back naive, and a mocked session proves nothing about either.

The engine is module-local so committed rows never leak into the shared
session-scoped `sqlite_engine` other suites roll back against.
"""

import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

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
from app.workers import schema_registry
from sqlalchemy import select
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
_USER_ID = uuid.UUID("f1e2d3c4-b5a6-4798-8a9b-0c1d2e3f4a5b")


@dataclass
class _StubDispatcher:
    """Only the surface `_sweep_stale_running_once` is allowed to touch."""

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


async def _seed_running_job(
    factory: async_sessionmaker[AsyncSession],
    *,
    age_seconds: float,
    retry_count: int = 0,
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
                    payload={"rows": 10, "__traceparent": {"traceparent": "x"}},
                    retry_count=retry_count,
                    max_retries=3,
                    trace_id="trace-abc",
                    started_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
                )
            )
    return job_id


async def _job(
    factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> Job:
    async with factory() as session:
        job = (
            await session.execute(select(Job).where(Job.id == job_id))
        ).scalar_one()
        return job


async def test_orphaned_running_job_is_dead_lettered_with_event_and_audit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """THE assertion that would have caught E1-17.

    A RUNNING row twice as old as the threshold, owned by nobody in this
    process (empty in-flight set = the owning worker is dead), must not
    survive one sweep pass as RUNNING.
    """
    job_id = await _seed_running_job(
        session_factory, age_seconds=2 * THRESHOLD_SECONDS
    )
    dispatcher = _StubDispatcher()

    recovered = await dispatcher_mod._sweep_stale_running_once(
        session_factory, dispatcher, THRESHOLD_SECONDS
    )
    assert recovered == 1

    job = await _job(session_factory, job_id)
    assert job.status == JobStatus.DEAD_LETTER
    assert job.status != JobStatus.RUNNING
    assert job.completed_at is not None
    assert job.error_message is not None
    assert "worker crash recovery" in job.error_message

    async with session_factory() as session:
        audit_rows = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.job_id == job_id,
                    AuditLog.action == "job.dead_letter",
                )
            )
        ).scalars().all()
        outbox_rows = (
            await session.execute(select(OutboxEvent).where(OutboxEvent.topic == "job.dlq"))
        ).scalars().all()

    assert len(audit_rows) == 1
    assert audit_rows[0].extra_data is not None
    assert audit_rows[0].extra_data["reason"] == "worker_crash_recovery"
    assert audit_rows[0].extra_data["stale_seconds"] >= THRESHOLD_SECONDS

    assert len(outbox_rows) == 1
    payload = outbox_rows[0].payload
    assert payload["dead_lettered"] is True
    assert payload["job_id"] == str(job_id)
    assert payload["job_type"] == JobType.CSV_UPLOAD
    assert payload["error"]
    # The outbox relay validates on publish; a schema-invalid row would rot
    # in `outbox_events` with `attempts` incrementing and no event fanning
    # out to triage / the saga coordinator.
    schema_registry.validate("job.dlq", payload)
    # Tracing plumbing must not leak into the event (or the job_events row
    # the event-log consumer appends verbatim).
    assert "__traceparent" not in payload["payload"]


async def test_in_flight_and_fresh_running_jobs_are_untouched(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two exclusions, both mandatory: this process's own live work, and
    anything younger than the threshold."""
    live_id = await _seed_running_job(
        session_factory, age_seconds=2 * THRESHOLD_SECONDS
    )
    fresh_id = await _seed_running_job(
        session_factory, age_seconds=THRESHOLD_SECONDS / 2
    )
    dispatcher = _StubDispatcher(in_flight_job_ids={str(live_id)})

    recovered = await dispatcher_mod._sweep_stale_running_once(
        session_factory, dispatcher, THRESHOLD_SECONDS
    )

    assert recovered == 0
    assert (await _job(session_factory, live_id)).status == JobStatus.RUNNING
    assert (await _job(session_factory, fresh_id)).status == JobStatus.RUNNING

    async with session_factory() as session:
        outbox_count = len(
            (await session.execute(select(OutboxEvent))).scalars().all()
        )
    assert outbox_count == 0


async def test_retry_count_is_preserved_through_dead_letter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A crash recovery is not a replay: replay deliberately resets
    `retry_count`, and zeroing it here would erase the attempt history the
    DLQ tab and triage reason about."""
    job_id = await _seed_running_job(
        session_factory, age_seconds=2 * THRESHOLD_SECONDS, retry_count=2
    )

    await dispatcher_mod._sweep_stale_running_once(
        session_factory, _StubDispatcher(), THRESHOLD_SECONDS
    )

    job = await _job(session_factory, job_id)
    assert job.status == JobStatus.DEAD_LETTER
    assert job.retry_count == 2

    async with session_factory() as session:
        outbox = (
            await session.execute(select(OutboxEvent))
        ).scalars().one()
    assert outbox.payload["retry_count"] == 2


async def test_running_row_without_started_at_is_skipped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A legacy / hand-seeded RUNNING row with no `started_at` has no age to
    measure — it must be skipped, not crash the loop."""
    job_id = uuid.uuid4()
    async with session_factory() as session:
        async with session.begin():
            session.add(
                Job(
                    id=job_id,
                    tenant_id=DEFAULT_TENANT_ID,
                    user_id=_USER_ID,
                    type=JobType.CSV_UPLOAD,
                    status=JobStatus.RUNNING,
                    started_at=None,
                )
            )

    recovered = await dispatcher_mod._sweep_stale_running_once(
        session_factory, _StubDispatcher(), THRESHOLD_SECONDS
    )

    assert recovered == 0
    assert (await _job(session_factory, job_id)).status == JobStatus.RUNNING


def test_dispatcher_tracks_in_flight_job_ids() -> None:
    """The exclusion set the sweep reads has to actually exist on the
    consumer, and start empty."""
    from unittest.mock import MagicMock

    consumer = dispatcher_mod.JobDispatcherConsumer(MagicMock(), MagicMock())
    assert consumer.in_flight_job_ids == set()


async def test_handle_message_claims_id_before_spawning_and_releases_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The id is claimed before the task is spawned (no window in which the
    sweep sees a just-dispatched job as an orphan) and dropped when the task
    settles."""
    import asyncio
    from unittest.mock import MagicMock

    consumer = dispatcher_mod.JobDispatcherConsumer(MagicMock(), MagicMock())
    job_id_str = str(uuid.uuid4())
    started = asyncio.Event()
    release = asyncio.Event()

    async def _fake_run_job(_jid: str, _factory: object, _redis: object) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(dispatcher_mod, "_run_job", _fake_run_job)
    try:
        await consumer.handle_message("job.submitted", "k", {"job_id": job_id_str})
        # Claimed synchronously by handle_message, before _run_job ever ran.
        assert job_id_str in consumer.in_flight_job_ids
        assert not started.is_set()

        await started.wait()
        assert job_id_str in consumer.in_flight_job_ids
        release.set()
        await asyncio.gather(*consumer.in_flight)
        assert job_id_str not in consumer.in_flight_job_ids
    finally:
        release.set()
