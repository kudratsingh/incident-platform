"""Unit tests for the outbox repository and relay loop."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.repositories.outbox import OutboxRepository
from app.workers import dispatcher

# ---------------------------------------------------------------------------
# OutboxRepository
# ---------------------------------------------------------------------------


async def test_repo_add_creates_row() -> None:
    session = AsyncMock()
    repo = OutboxRepository(session)
    # BaseRepository.create() calls session.add (sync) then session.flush/refresh
    session.add = MagicMock()
    tenant_id = uuid.uuid4()

    await repo.add(
        tenant_id=tenant_id, topic="job.submitted", key="user-1", payload={"x": 1}
    )

    session.add.assert_called_once()
    session.flush.assert_awaited()
    instance = session.add.call_args.args[0]
    assert instance.tenant_id == tenant_id
    assert instance.topic == "job.submitted"
    assert instance.key == "user-1"
    assert instance.payload == {"x": 1}


async def test_repo_mark_published_noop_on_empty_list() -> None:
    session = AsyncMock()
    repo = OutboxRepository(session)
    await repo.mark_published([])
    session.execute.assert_not_awaited()


async def test_repo_mark_published_executes_when_ids_given() -> None:
    session = AsyncMock()
    repo = OutboxRepository(session)
    await repo.mark_published([uuid.uuid4(), uuid.uuid4()])
    session.execute.assert_awaited_once()
    session.flush.assert_awaited_once()


# ---------------------------------------------------------------------------
# _outbox_relay_loop
# ---------------------------------------------------------------------------


def _session_factory_with(events: list[MagicMock]) -> tuple[MagicMock, MagicMock]:
    """Build a session_factory and OutboxRepository mock that yields `events`
    on the first poll, then [] forever after — so the loop processes one batch."""
    repo = AsyncMock()
    repo.fetch_unpublished.side_effect = [events] + [[]] * 100
    repo.mark_published = AsyncMock()
    repo.increment_attempts = AsyncMock()

    begin_ctx = MagicMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_ctx)

    factory = MagicMock(return_value=session)
    return factory, repo


def _outbox_row(topic: str = "job.submitted", payload: dict | None = None) -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.topic = topic
    row.key = "user-1"
    row.payload = payload or {"job_id": str(uuid.uuid4())}
    return row


async def test_relay_publishes_and_marks_each_event() -> None:
    events = [_outbox_row(), _outbox_row()]
    factory, repo = _session_factory_with(events)

    publish_mock = AsyncMock()

    with patch("app.workers.dispatcher.OutboxRepository", return_value=repo), \
         patch("app.workers.dispatcher.kafka_producer.publish_raw", new=publish_mock), \
         patch(
             "app.workers.dispatcher.asyncio.sleep",
             side_effect=[None, StopAsyncIteration],
         ):
        # First sleep returns (lets the empty-batch tick run), second raises to exit.
        with pytest.raises(StopAsyncIteration):
            await dispatcher._outbox_relay_loop(factory)

    assert publish_mock.await_count == 2
    repo.mark_published.assert_awaited()
    published_ids = repo.mark_published.await_args_list[0].args[0]
    assert set(published_ids) == {e.id for e in events}


async def test_relay_leaves_failed_rows_unpublished() -> None:
    """Rows whose publish raises must NOT be marked published — they retry next tick."""
    good = _outbox_row()
    bad = _outbox_row()
    factory, repo = _session_factory_with([good, bad])

    async def publish_side_effect(topic: str, key: str, payload: dict) -> None:
        if payload is bad.payload:
            raise RuntimeError("broker down")

    with patch("app.workers.dispatcher.OutboxRepository", return_value=repo), \
         patch(
             "app.workers.dispatcher.kafka_producer.publish_raw",
             side_effect=publish_side_effect,
         ), \
         patch(
             "app.workers.dispatcher.asyncio.sleep",
             side_effect=[None, StopAsyncIteration],
         ):
        with pytest.raises(StopAsyncIteration):
            await dispatcher._outbox_relay_loop(factory)

    published_ids = repo.mark_published.await_args_list[0].args[0]
    failed_ids = repo.increment_attempts.await_args_list[0].args[0]
    assert published_ids == [good.id]
    assert failed_ids == [bad.id]


async def test_relay_sleeps_when_outbox_is_empty() -> None:
    factory, repo = _session_factory_with([])

    with patch("app.workers.dispatcher.OutboxRepository", return_value=repo), \
         patch("app.workers.dispatcher.kafka_producer.publish_raw", new=AsyncMock()) as pub, \
         patch("app.workers.dispatcher.asyncio.sleep", side_effect=StopAsyncIteration):
        with pytest.raises(StopAsyncIteration):
            await dispatcher._outbox_relay_loop(factory)

    pub.assert_not_awaited()
    repo.mark_published.assert_not_awaited()


# ---------------------------------------------------------------------------
# JobService writes outbox row alongside the job row
# ---------------------------------------------------------------------------


async def test_job_service_create_writes_outbox() -> None:
    from app.models.enums import JobStatus, JobType
    from app.models.job import Job
    from app.services.job import JobService

    job_repo = AsyncMock()
    audit_repo = AsyncMock()
    outbox_repo = AsyncMock()
    redis = AsyncMock()

    job_repo.get_by_idempotency_key.return_value = None
    job = MagicMock(spec=Job)
    job.id = uuid.uuid4()
    job.trace_id = "trace-x"
    job.user_id = uuid.uuid4()
    job.status = JobStatus.PENDING
    job_repo.create.return_value = job

    svc = JobService(job_repo, audit_repo, outbox_repo, redis)
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    await svc.create_job(
        user_id=user_id, tenant_id=tenant_id, job_type=JobType.CSV_UPLOAD
    )

    outbox_repo.add.assert_awaited_once()
    kwargs = outbox_repo.add.await_args.kwargs
    assert kwargs["tenant_id"] == tenant_id
    assert kwargs["topic"] == "job.submitted"
    # Composite partition key — tenants spread across partitions, ordering
    # preserved per (tenant, user).
    assert kwargs["key"] == f"{tenant_id}:{user_id}"
    assert kwargs["payload"]["event"] == "job.submitted"
    assert kwargs["payload"]["tenant_id"] == str(tenant_id)
    assert kwargs["payload"]["job_id"] == str(job.id)
