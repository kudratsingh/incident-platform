"""Unit tests for JobService — repositories are mocked, no DB needed."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.core.exceptions import AuthorizationError, JobError, NotFoundError
from app.models.enums import JobStatus, JobType, UserRole
from app.models.job import Job
from app.services.job import JobService


def _make_job(**kwargs: object) -> Job:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "type": JobType.CSV_UPLOAD,
        "status": JobStatus.PENDING,
        "idempotency_key": None,
        "retry_count": 0,
        "max_retries": 3,
        "priority": 0,
        "trace_id": None,
        "payload": None,
        "result": None,
        "error_message": None,
    }
    defaults.update(kwargs)
    job = MagicMock(spec=Job)
    for k, v in defaults.items():
        setattr(job, k, v)
    return job  # type: ignore[return-value]


def _make_service() -> tuple[JobService, AsyncMock, AsyncMock, AsyncMock]:
    job_repo = AsyncMock()
    audit_repo = AsyncMock()
    outbox_repo = AsyncMock()
    redis = AsyncMock()
    svc = JobService(job_repo, audit_repo, outbox_repo, redis)
    return svc, job_repo, audit_repo, outbox_repo


# ---------------------------------------------------------------------------
# create_job
# ---------------------------------------------------------------------------


async def test_create_job_success() -> None:
    svc, job_repo, audit_repo, _ = _make_service()
    job_repo.get_by_idempotency_key.return_value = None
    new_job = _make_job()
    job_repo.create.return_value = new_job

    result = await svc.create_job(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        job_type=JobType.CSV_UPLOAD,
    )

    job_repo.create.assert_awaited_once()
    audit_repo.log.assert_awaited_once()
    assert result is new_job


async def test_create_job_idempotency_returns_existing() -> None:
    svc, job_repo, _, _ = _make_service()
    existing = _make_job(idempotency_key="key-123")
    job_repo.get_by_idempotency_key.return_value = existing

    result = await svc.create_job(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        job_type=JobType.CSV_UPLOAD,
        idempotency_key="key-123",
    )

    job_repo.create.assert_not_awaited()
    assert result is existing


# ---------------------------------------------------------------------------
# get_job
# ---------------------------------------------------------------------------


async def test_get_job_owner_can_access() -> None:
    svc, job_repo, _, _ = _make_service()
    owner_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    job = _make_job(user_id=owner_id, tenant_id=tenant_id)
    job_repo.get_for_tenant.return_value = job

    result = await svc.get_job(job.id, owner_id, UserRole.USER, tenant_id)
    assert result is job


async def test_get_job_non_owner_raises() -> None:
    svc, job_repo, _, _ = _make_service()
    tenant_id = uuid.uuid4()
    job = _make_job(user_id=uuid.uuid4(), tenant_id=tenant_id)
    job_repo.get_for_tenant.return_value = job

    with pytest.raises(AuthorizationError):
        await svc.get_job(job.id, uuid.uuid4(), UserRole.USER, tenant_id)


async def test_get_job_admin_can_access_any() -> None:
    svc, job_repo, _, _ = _make_service()
    tenant_id = uuid.uuid4()
    job = _make_job(user_id=uuid.uuid4(), tenant_id=tenant_id)
    job_repo.get_for_tenant.return_value = job

    result = await svc.get_job(job.id, uuid.uuid4(), UserRole.ADMIN, tenant_id)
    assert result is job


async def test_get_job_not_found_raises() -> None:
    svc, job_repo, _, _ = _make_service()
    job_repo.get_for_tenant.return_value = None

    with pytest.raises(NotFoundError):
        await svc.get_job(uuid.uuid4(), uuid.uuid4(), UserRole.ADMIN, uuid.uuid4())


# ---------------------------------------------------------------------------
# replay_job
# ---------------------------------------------------------------------------


async def test_replay_failed_job() -> None:
    svc, job_repo, audit_repo, _ = _make_service()
    tenant_id = uuid.uuid4()
    failed_job = _make_job(status=JobStatus.FAILED, tenant_id=tenant_id)
    replayed_job = _make_job(
        status=JobStatus.PENDING, id=failed_job.id, tenant_id=tenant_id
    )
    job_repo.get_for_tenant.return_value = failed_job
    job_repo.update_status.return_value = replayed_job

    result = await svc.replay_job(failed_job.id, uuid.uuid4(), tenant_id)

    job_repo.update_status.assert_awaited_once()
    audit_repo.log.assert_awaited_once()
    assert result is replayed_job


async def test_replay_resets_retry_count_and_error() -> None:
    """A DLQ'd job at max retries must get a clean retry budget on replay,
    or the next failure dead-letters it again immediately."""
    svc, job_repo, audit_repo, _ = _make_service()
    tenant_id = uuid.uuid4()
    dead_job = _make_job(
        status=JobStatus.DEAD_LETTER,
        retry_count=3,
        max_retries=3,
        error_message="last attempt boom",
        tenant_id=tenant_id,
    )
    job_repo.get_for_tenant.return_value = dead_job
    job_repo.update_status.return_value = dead_job

    await svc.replay_job(dead_job.id, uuid.uuid4(), tenant_id)

    extra = job_repo.update_status.await_args.kwargs["extra"]
    assert extra["retry_count"] == 0
    assert extra["error_message"] is None
    assert extra["result"] is None

    audit_extra = audit_repo.log.await_args.kwargs["extra_data"]
    assert audit_extra["previous_retry_count"] == 3
    assert audit_extra["previous_status"] == JobStatus.DEAD_LETTER


async def test_replay_non_failed_job_raises() -> None:
    svc, job_repo, _, _ = _make_service()
    tenant_id = uuid.uuid4()
    running_job = _make_job(status=JobStatus.RUNNING, tenant_id=tenant_id)
    job_repo.get_for_tenant.return_value = running_job

    with pytest.raises(JobError, match="replayed"):
        await svc.replay_job(running_job.id, uuid.uuid4(), tenant_id)
