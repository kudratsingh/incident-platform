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


def _comp_job_repo(failed: MagicMock) -> AsyncMock:
    """JobRepository mock whose `create` mints a row with a real id, the way
    BaseRepository.create does (add + flush + refresh) — so tests can compare
    the published job_id against the id of the row that was actually created."""
    repo = AsyncMock()
    repo.get_by_id.return_value = failed
    created: list[MagicMock] = []

    def _create(**kwargs: object) -> MagicMock:
        row = MagicMock()
        row.id = uuid.uuid4()
        for k, v in kwargs.items():
            setattr(row, k, v)
        created.append(row)
        return row

    repo.create.side_effect = _create
    repo.created_rows = created
    return repo


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
    # waiting_steps() filters on WAITING/PENDING — by job.dlq time the failed
    # step is DEAD_LETTER, so the real query can never return it.
    saga_repo.waiting_steps.return_value = [waiting]
    job_repo = _comp_job_repo(failed)
    outbox = AsyncMock()
    audit = AsyncMock()

    with patch("app.workers.saga_coordinator.JobRepository", return_value=job_repo), \
         patch("app.workers.saga_coordinator.SagaRepository", return_value=saga_repo), \
         patch("app.workers.saga_coordinator.OutboxRepository", return_value=outbox), \
         patch("app.workers.saga_coordinator.AuditRepository", return_value=audit):
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

    # E1-13: the audit count is the number of cancellations that actually
    # happened, not len(waiting_steps) - 1.
    extra = audit.log.await_args.kwargs["extra_data"]
    assert extra["cancelled_downstream"] == 1


async def test_cancelled_downstream_is_zero_when_nothing_was_waiting() -> None:
    """E1-13 repro: with no downstream steps the audit row claimed -1
    cancellations (len(waiting) - 1 on an empty list)."""
    factory = _factory()
    coord = SagaCoordinator(factory)
    saga = _saga()

    failed = _job_in_saga(saga.id, JobStatus.DEAD_LETTER)
    completed_a = _job_in_saga(saga.id, JobStatus.COMPLETED)
    completed_a.type = "step_a"

    saga_repo = AsyncMock()
    saga_repo.get_by_id.return_value = saga
    saga_repo.completed_steps.return_value = [completed_a]
    saga_repo.waiting_steps.return_value = []
    job_repo = _comp_job_repo(failed)
    audit = AsyncMock()

    with patch("app.workers.saga_coordinator.JobRepository", return_value=job_repo), \
         patch("app.workers.saga_coordinator.SagaRepository", return_value=saga_repo), \
         patch("app.workers.saga_coordinator.OutboxRepository", return_value=AsyncMock()), \
         patch("app.workers.saga_coordinator.AuditRepository", return_value=audit):
        await coord.handle_message(
            topic="job.dlq",
            key="u",
            value={
                "event": "job.failed",
                "job_id": str(failed.id),
                "dead_lettered": True,
            },
        )

    extra = audit.log.await_args.kwargs["extra_data"]
    assert extra["cancelled_downstream"] == 0


async def test_compensation_creates_real_job_rows_matching_the_outbox() -> None:
    """E1-02: compensation steps must be real `jobs` rows created in the same
    transaction as the outbox row, and the published job_id must be that row's
    id — a fresh uuid4 makes the dispatcher log 'job not found, skipping'."""
    factory = _factory()
    coord = SagaCoordinator(factory)
    saga = _saga()

    failed = _job_in_saga(saga.id, JobStatus.DEAD_LETTER)
    completed_a = _job_in_saga(saga.id, JobStatus.COMPLETED)
    completed_a.type = "step_a"
    completed_b = _job_in_saga(saga.id, JobStatus.COMPLETED)
    completed_b.type = "step_b"

    saga_repo = AsyncMock()
    saga_repo.get_by_id.return_value = saga
    saga_repo.completed_steps.return_value = [completed_a, completed_b]
    saga_repo.waiting_steps.return_value = []
    job_repo = _comp_job_repo(failed)
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

    # One real jobs row per already-completed step, attached to the saga.
    create_calls = job_repo.create.await_args_list
    assert len(create_calls) == 2
    assert [c.kwargs["type"] for c in create_calls] == [
        f"step_b{COMPENSATE_SUFFIX}",
        f"step_a{COMPENSATE_SUFFIX}",
    ]
    for call in create_calls:
        assert call.kwargs["saga_id"] == saga.id
        assert call.kwargs["status"] == JobStatus.PENDING
        assert call.kwargs["tenant_id"] == saga.tenant_id
        assert call.kwargs["trace_id"] == "t"
    assert create_calls[0].kwargs["payload"] == {
        "compensates_job_id": str(completed_b.id),
        "saga_id": str(saga.id),
    }

    # Every published job.submitted names the row that was just created.
    created_ids = [str(row.id) for row in job_repo.created_rows]
    published_ids = [
        c.kwargs["payload"]["job_id"] for c in outbox.add.await_args_list
    ]
    assert published_ids == created_ids


async def test_compensation_settles_saga_failed_when_a_comp_step_dead_letters()\
        -> None:
    factory = _factory()
    coord = SagaCoordinator(factory)
    saga = _saga(SagaStatus.COMPENSATING)

    comp = _job_in_saga(saga.id, JobStatus.DEAD_LETTER)
    comp.type = f"csv_upload{COMPENSATE_SUFFIX}"
    original = _job_in_saga(saga.id, JobStatus.DEAD_LETTER)

    saga_repo = AsyncMock()
    saga_repo.get_by_id.return_value = saga
    saga_repo.jobs.return_value = [original, comp]
    job_repo = AsyncMock()
    job_repo.get_by_id.return_value = comp
    audit = AsyncMock()

    with patch("app.workers.saga_coordinator.JobRepository", return_value=job_repo), \
         patch("app.workers.saga_coordinator.SagaRepository", return_value=saga_repo), \
         patch("app.workers.saga_coordinator.AuditRepository", return_value=audit):
        await coord.handle_message(
            topic="job.dlq",
            key="u",
            value={
                "event": "job.failed",
                "job_id": str(comp.id),
                "dead_lettered": True,
            },
        )

    assert saga.status == SagaStatus.FAILED
    assert saga.completed_at is not None
    assert audit.log.await_args.args[0] == "saga.compensation_failed"
    assert audit.log.await_args.kwargs["extra_data"] == {
        "compensation_steps": 1,
        "dead_lettered": 1,
    }


async def test_compensation_settles_saga_compensated_when_all_comp_steps_done()\
        -> None:
    factory = _factory()
    coord = SagaCoordinator(factory)
    saga = _saga(SagaStatus.COMPENSATING)

    comp = _job_in_saga(saga.id, JobStatus.COMPLETED)
    comp.type = f"csv_upload{COMPENSATE_SUFFIX}"
    original = _job_in_saga(saga.id, JobStatus.DEAD_LETTER)

    saga_repo = AsyncMock()
    saga_repo.get_by_id.return_value = saga
    saga_repo.jobs.return_value = [original, comp]
    job_repo = AsyncMock()
    job_repo.get_by_id.return_value = comp
    audit = AsyncMock()

    with patch("app.workers.saga_coordinator.JobRepository", return_value=job_repo), \
         patch("app.workers.saga_coordinator.SagaRepository", return_value=saga_repo), \
         patch("app.workers.saga_coordinator.AuditRepository", return_value=audit):
        await coord.handle_message(
            topic="job.completed",
            key="u",
            value={"event": "job.completed", "job_id": str(comp.id)},
        )

    assert saga.status == SagaStatus.COMPENSATED
    assert saga.completed_at is not None
    assert audit.log.await_args.args[0] == "saga.compensated"


async def test_compensation_leaves_saga_compensating_while_a_step_is_pending()\
        -> None:
    factory = _factory()
    coord = SagaCoordinator(factory)
    saga = _saga(SagaStatus.COMPENSATING)

    done_comp = _job_in_saga(saga.id, JobStatus.COMPLETED)
    done_comp.type = f"step_b{COMPENSATE_SUFFIX}"
    pending_comp = _job_in_saga(saga.id, JobStatus.PENDING)
    pending_comp.type = f"step_a{COMPENSATE_SUFFIX}"

    saga_repo = AsyncMock()
    saga_repo.get_by_id.return_value = saga
    saga_repo.jobs.return_value = [done_comp, pending_comp]
    job_repo = AsyncMock()
    job_repo.get_by_id.return_value = done_comp
    audit = AsyncMock()

    with patch("app.workers.saga_coordinator.JobRepository", return_value=job_repo), \
         patch("app.workers.saga_coordinator.SagaRepository", return_value=saga_repo), \
         patch("app.workers.saga_coordinator.AuditRepository", return_value=audit):
        await coord.handle_message(
            topic="job.completed",
            key="u",
            value={"event": "job.completed", "job_id": str(done_comp.id)},
        )

    assert saga.status == SagaStatus.COMPENSATING
    assert saga.completed_at is None
    audit.log.assert_not_awaited()


async def test_redelivered_original_step_dlq_does_not_recompensate() -> None:
    """At-least-once redelivery of the ORIGINAL step's job.dlq must not
    re-enter _handle_failure and mint a second set of compensation rows."""
    factory = _factory()
    coord = SagaCoordinator(factory)
    saga = _saga(SagaStatus.COMPENSATING)

    original = _job_in_saga(saga.id, JobStatus.DEAD_LETTER)  # type "csv_upload"

    saga_repo = AsyncMock()
    saga_repo.get_by_id.return_value = saga
    job_repo = AsyncMock()
    job_repo.get_by_id.return_value = original
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
                "job_id": str(original.id),
                "dead_lettered": True,
            },
        )

    assert saga.status == SagaStatus.COMPENSATING
    job_repo.create.assert_not_awaited()
    outbox.add.assert_not_awaited()
    saga_repo.jobs.assert_not_awaited()


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
