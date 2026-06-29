"""Unit tests for the worker dispatcher — DB and Redis fully mocked."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.enums import JobStatus, JobType
from app.models.job import Job
from app.workers import dispatcher


def _make_job(**kwargs: object) -> MagicMock:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "type": JobType.BULK_API_SYNC,
        "status": JobStatus.PENDING,
        "payload": {},
        "retry_count": 0,
        "max_retries": 3,
        "trace_id": None,
    }
    defaults.update(kwargs)
    job = MagicMock(spec=Job)
    for k, v in defaults.items():
        setattr(job, k, v)
    return job


def _make_session_factory(job: MagicMock) -> MagicMock:
    """Returns a session factory whose sessions yield a job_repo that returns `job`."""
    job_repo = AsyncMock()
    job_repo.get_by_id.return_value = job
    job_repo.update_status.return_value = job

    audit_repo = AsyncMock()
    audit_repo.log = AsyncMock()

    # session.begin() must be a regular (sync) call returning an async context manager
    begin_ctx = MagicMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_ctx)

    factory = MagicMock()
    factory.return_value = session

    return factory, job_repo, audit_repo


async def test_run_job_success_marks_completed() -> None:
    job = _make_job(type=JobType.BULK_API_SYNC)
    factory, job_repo, audit_repo = _make_session_factory(job)
    redis = AsyncMock()

    processor = AsyncMock(return_value={"ok": True})
    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=AsyncMock()),
         ), \
         patch.dict(dispatcher._PROCESSORS, {JobType.BULK_API_SYNC: processor}):
        await dispatcher._run_job(str(job.id), factory, redis)

    calls = [c.args[1] for c in job_repo.update_status.call_args_list]
    assert JobStatus.RUNNING in calls
    assert JobStatus.COMPLETED in calls


async def test_run_job_retries_on_failure() -> None:
    job = _make_job(type=JobType.BULK_API_SYNC, retry_count=0, max_retries=3)
    factory, job_repo, audit_repo = _make_session_factory(job)
    redis = AsyncMock()

    processor = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=AsyncMock()),
         ), \
         patch.dict(dispatcher._PROCESSORS, {JobType.BULK_API_SYNC: processor}), \
         patch("app.workers.dispatcher.queue.push_delayed", new=AsyncMock()) as mock_delay:
        await dispatcher._run_job(str(job.id), factory, redis)

    mock_delay.assert_awaited_once()
    calls = [c.args[1] for c in job_repo.update_status.call_args_list]
    assert JobStatus.PENDING in calls


async def test_run_job_dead_letters_after_exhaustion() -> None:
    job = _make_job(type=JobType.BULK_API_SYNC, retry_count=2, max_retries=3)
    factory, job_repo, audit_repo = _make_session_factory(job)
    redis = AsyncMock()

    processor = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=AsyncMock()),
         ), \
         patch.dict(dispatcher._PROCESSORS, {JobType.BULK_API_SYNC: processor}), \
         patch("app.workers.dispatcher.queue.push_delayed", new=AsyncMock()) as mock_delay:
        await dispatcher._run_job(str(job.id), factory, redis)

    mock_delay.assert_not_awaited()
    calls = [c.args[1] for c in job_repo.update_status.call_args_list]
    assert JobStatus.DEAD_LETTER in calls


async def test_run_job_llm_policy_forces_dead_letter_before_exhaustion() -> None:
    """When the LLM-guided policy says dead_letter_now, the dispatcher must
    honor it even though there are deterministic retries remaining."""
    from app.services.retry_policy import RetryDecision

    job = _make_job(type=JobType.BULK_API_SYNC, retry_count=1, max_retries=5)
    factory, job_repo, audit_repo = _make_session_factory(job)
    redis = AsyncMock()

    processor = AsyncMock(side_effect=RuntimeError("401 Unauthorized"))
    fake_decision = RetryDecision(
        action="dead_letter_now",
        backoff_seconds=0,
        reasoning="Auth failure won't recover.",
    )
    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=AsyncMock()),
         ), \
         patch.dict(dispatcher._PROCESSORS, {JobType.BULK_API_SYNC: processor}), \
         patch(
             "app.workers.dispatcher.retry_policy.is_enabled", return_value=True
         ), \
         patch(
             "app.workers.dispatcher.retry_policy.decide_retry",
             new=AsyncMock(return_value=(fake_decision, {}, "claude-opus-4-7")),
         ), \
         patch("app.workers.dispatcher.queue.push_delayed", new=AsyncMock()) as mock_delay:
        await dispatcher._run_job(str(job.id), factory, redis)

    # Did NOT enqueue another retry — went straight to DLQ.
    mock_delay.assert_not_awaited()
    calls = [c.args[1] for c in job_repo.update_status.call_args_list]
    assert JobStatus.DEAD_LETTER in calls


async def test_run_job_llm_policy_failure_falls_back_to_deterministic() -> None:
    """If the LLM call raises (timeout, network, schema mismatch), the
    deterministic exponential-backoff retry still happens — the worker
    can never block on the API being unhealthy."""
    job = _make_job(type=JobType.BULK_API_SYNC, retry_count=1, max_retries=5)
    factory, job_repo, audit_repo = _make_session_factory(job)
    redis = AsyncMock()

    processor = AsyncMock(side_effect=RuntimeError("HTTP 500"))
    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=AsyncMock()),
         ), \
         patch.dict(dispatcher._PROCESSORS, {JobType.BULK_API_SYNC: processor}), \
         patch(
             "app.workers.dispatcher.retry_policy.is_enabled", return_value=True
         ), \
         patch(
             "app.workers.dispatcher.retry_policy.decide_retry",
             new=AsyncMock(side_effect=RuntimeError("API down")),
         ), \
         patch("app.workers.dispatcher.queue.push_delayed", new=AsyncMock()) as mock_delay:
        await dispatcher._run_job(str(job.id), factory, redis)

    mock_delay.assert_awaited_once()
    calls = [c.args[1] for c in job_repo.update_status.call_args_list]
    assert JobStatus.PENDING in calls
    assert JobStatus.DEAD_LETTER not in calls


async def test_run_job_skips_unknown_job() -> None:
    begin_ctx = MagicMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_ctx)

    factory = MagicMock()
    factory.return_value = session

    job_repo = AsyncMock()
    job_repo.get_by_id.return_value = None

    redis = AsyncMock()

    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo):
        await dispatcher._run_job(str(uuid.uuid4()), factory, redis)

    job_repo.update_status.assert_not_awaited()


# ---------------------------------------------------------------------------
# JobDispatcherConsumer
# ---------------------------------------------------------------------------


async def test_dispatcher_consumer_spawns_run_job_for_valid_message() -> None:
    factory = MagicMock()
    redis = AsyncMock()
    consumer = dispatcher.JobDispatcherConsumer(factory, redis)
    job_id_str = str(uuid.uuid4())

    with patch("app.workers.dispatcher._run_job", new=AsyncMock()) as mock_run:
        await consumer.handle_message(
            topic="job.submitted",
            key="user-1",
            value={"job_id": job_id_str, "user_id": "user-1", "job_type": "csv_upload"},
        )
        # Let the spawned background task run to completion
        await asyncio.gather(*consumer.in_flight, return_exceptions=True)

    mock_run.assert_awaited_once_with(job_id_str, factory, redis)


async def test_dispatcher_consumer_skips_malformed_message() -> None:
    factory = MagicMock()
    redis = AsyncMock()
    consumer = dispatcher.JobDispatcherConsumer(factory, redis)

    with patch("app.workers.dispatcher._run_job", new=AsyncMock()) as mock_run:
        # Missing job_id — must return without raising and without dispatching.
        await consumer.handle_message(
            topic="job.submitted", key=None, value={"user_id": "x"}
        )

    mock_run.assert_not_awaited()
    assert not consumer.in_flight


async def test_dispatcher_consumer_semaphore_releases_on_run_failure() -> None:
    """If _run_job raises, the semaphore must still release so we don't deadlock."""
    factory = MagicMock()
    redis = AsyncMock()
    consumer = dispatcher.JobDispatcherConsumer(factory, redis, max_concurrent=1)

    with patch(
        "app.workers.dispatcher._run_job",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        await consumer.handle_message(
            topic="job.submitted", key="u", value={"job_id": str(uuid.uuid4())}
        )
        await asyncio.gather(*consumer.in_flight, return_exceptions=True)

    # Semaphore should be back at 1 — i.e. a fresh acquire returns immediately.
    assert consumer.semaphore.locked() is False
