"""Unit tests for the SagaCoordinator consumer."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.enums import JobStatus, SagaStatus
from app.workers.saga_coordinator import COMPENSATE_SUFFIX, SagaCoordinator


def _factory() -> MagicMock:
    begin_ctx = MagicMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_ctx)
    return MagicMock(return_value=session)


def _job_in_saga(saga_id: uuid.UUID, status: str = JobStatus.COMPLETED) -> MagicMock:
    j = MagicMock()
    j.id = uuid.uuid4()
    j.saga_id = saga_id
    j.user_id = uuid.uuid4()
    j.type = "csv_upload"
    j.status = status
    j.priority = 0
    j.trace_id = "t"
    return j


def _saga(status: str = SagaStatus.RUNNING) -> MagicMock:
    s = MagicMock()
    s.id = uuid.uuid4()
    s.status = status
    s.completed_at = None
    return s


async def test_completion_marks_saga_completed_when_all_done() -> None:
    factory = _factory()
    coord = SagaCoordinator(factory)
    saga = _saga()
    failing_step = _job_in_saga(saga.id)

    saga_repo = AsyncMock()
    saga_repo.get_by_id.return_value = saga
    saga_repo.jobs.return_value = [
        _job_in_saga(saga.id, JobStatus.COMPLETED),
        _job_in_saga(saga.id, JobStatus.COMPLETED),
    ]
    job_repo = AsyncMock()
    job_repo.get_by_id.return_value = failing_step
    audit = AsyncMock()

    with patch("app.workers.saga_coordinator.JobRepository", return_value=job_repo), \
         patch("app.workers.saga_coordinator.SagaRepository", return_value=saga_repo), \
         patch("app.workers.saga_coordinator.AuditRepository", return_value=audit):
        await coord.handle_message(
            topic="job.completed",
            key="u",
            value={"event": "job.completed", "job_id": str(failing_step.id)},
        )

    assert saga.status == SagaStatus.COMPLETED
    assert saga.completed_at is not None
    audit.log.assert_awaited_once()


async def test_completion_leaves_saga_running_if_steps_remain() -> None:
    factory = _factory()
    coord = SagaCoordinator(factory)
    saga = _saga()
    one_step = _job_in_saga(saga.id)

    saga_repo = AsyncMock()
    saga_repo.get_by_id.return_value = saga
    saga_repo.jobs.return_value = [
        _job_in_saga(saga.id, JobStatus.COMPLETED),
        _job_in_saga(saga.id, JobStatus.WAITING),  # not done yet
    ]
    job_repo = AsyncMock()
    job_repo.get_by_id.return_value = one_step

    with patch("app.workers.saga_coordinator.JobRepository", return_value=job_repo), \
         patch("app.workers.saga_coordinator.SagaRepository", return_value=saga_repo), \
         patch("app.workers.saga_coordinator.AuditRepository", return_value=AsyncMock()):
        await coord.handle_message(
            topic="job.completed",
            key="u",
            value={"event": "job.completed", "job_id": str(one_step.id)},
        )

    assert saga.status == SagaStatus.RUNNING


async def test_dlq_failure_cancels_downstream_and_emits_compensations() -> None:
    factory = _factory()
    coord = SagaCoordinator(factory)
    saga = _saga()

    failed = _job_in_saga(saga.id, JobStatus.DEAD_LETTER)
    completed_a = _job_in_saga(saga.id, JobStatus.COMPLETED)
    completed_a.type = "step_a"
    completed_b = _job_in_saga(saga.id, JobStatus.COMPLETED)
    completed_b.type = "step_b"
    waiting = _job_in_saga(saga.id, JobStatus.WAITING)

    saga_repo = AsyncMock()
    saga_repo.get_by_id.return_value = saga
    saga_repo.completed_steps.return_value = [completed_a, completed_b]
    saga_repo.waiting_steps.return_value = [waiting, failed]
    job_repo = AsyncMock()
    job_repo.get_by_id.return_value = failed
    outbox = AsyncMock()

    with patch("app.workers.saga_coordinator.JobRepository", return_value=job_repo), \
         patch("app.workers.saga_coordinator.SagaRepository", return_value=saga_repo), \
         patch("app.workers.saga_coordinator.OutboxRepository", return_value=outbox), \
         patch("app.workers.saga_coordinator.AuditRepository", return_value=AsyncMock()):
        await coord.handle_message(
            topic="job.dlq",
            key="u",
            value={
                "event": "job.failed",
                "job_id": str(failed.id),
                "dead_lettered": True,
            },
        )

    assert saga.status == SagaStatus.COMPENSATING

    # Waiting step (not the failed one) was cancelled.
    cancelled_calls = [
        c for c in job_repo.update_status.await_args_list
        if c.args[1] == JobStatus.CANCELLED
    ]
    assert len(cancelled_calls) == 1
    assert cancelled_calls[0].args[0] == waiting.id

    # Compensation jobs emitted in reverse order: step_b first, step_a second.
    compensate_types = [
        c.kwargs["payload"]["job_type"] for c in outbox.add.await_args_list
    ]
    assert compensate_types == [
        f"step_b{COMPENSATE_SUFFIX}",
        f"step_a{COMPENSATE_SUFFIX}",
    ]


async def test_ignores_event_for_non_saga_job() -> None:
    factory = _factory()
    coord = SagaCoordinator(factory)
    non_saga_job = MagicMock()
    non_saga_job.saga_id = None

    job_repo = AsyncMock()
    job_repo.get_by_id.return_value = non_saga_job
    saga_repo = AsyncMock()

    with patch("app.workers.saga_coordinator.JobRepository", return_value=job_repo), \
         patch("app.workers.saga_coordinator.SagaRepository", return_value=saga_repo):
        await coord.handle_message(
            topic="job.completed",
            key="u",
            value={"event": "job.completed", "job_id": str(uuid.uuid4())},
        )

    saga_repo.get_by_id.assert_not_awaited()
