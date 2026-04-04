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


def _make_service() -> tuple[JobService, AsyncMock, AsyncMock]:
    job_repo = AsyncMock()
    audit_repo = AsyncMock()
    svc = JobService(job_repo, audit_repo)
    return svc, job_repo, audit_repo


# ---------------------------------------------------------------------------
# create_job
# ---------------------------------------------------------------------------


async def test_create_job_success() -> None:
    svc, job_repo, audit_repo = _make_service()
    job_repo.get_by_idempotency_key.return_value = None
    new_job = _make_job()
    job_repo.create.return_value = new_job

    result = await svc.create_job(
        user_id=uuid.uuid4(), job_type=JobType.CSV_UPLOAD
    )

    job_repo.create.assert_awaited_once()
    audit_repo.log.assert_awaited_once()
    assert result is new_job


async def test_create_job_idempotency_returns_existing() -> None:
    svc, job_repo, _ = _make_service()
    existing = _make_job(idempotency_key="key-123")
    job_repo.get_by_idempotency_key.return_value = existing

    result = await svc.create_job(
        user_id=uuid.uuid4(),
        job_type=JobType.CSV_UPLOAD,
        idempotency_key="key-123",
    )

    job_repo.create.assert_not_awaited()
    assert result is existing


# ---------------------------------------------------------------------------
# get_job
# ---------------------------------------------------------------------------


async def test_get_job_owner_can_access() -> None:
    svc, job_repo, _ = _make_service()
    owner_id = uuid.uuid4()
    job = _make_job(user_id=owner_id)
    job_repo.get_by_id.return_value = job

    result = await svc.get_job(job.id, owner_id, UserRole.USER)
    assert result is job


async def test_get_job_non_owner_raises() -> None:
    svc, job_repo, _ = _make_service()
    job = _make_job(user_id=uuid.uuid4())
    job_repo.get_by_id.return_value = job

    with pytest.raises(AuthorizationError):
        await svc.get_job(job.id, uuid.uuid4(), UserRole.USER)


async def test_get_job_admin_can_access_any() -> None:
    svc, job_repo, _ = _make_service()
    job = _make_job(user_id=uuid.uuid4())
    job_repo.get_by_id.return_value = job

    result = await svc.get_job(job.id, uuid.uuid4(), UserRole.ADMIN)
    assert result is job


async def test_get_job_not_found_raises() -> None:
    svc, job_repo, _ = _make_service()
    job_repo.get_by_id.return_value = None

    with pytest.raises(NotFoundError):
        await svc.get_job(uuid.uuid4(), uuid.uuid4(), UserRole.ADMIN)


# ---------------------------------------------------------------------------
# replay_job
# ---------------------------------------------------------------------------


async def test_replay_failed_job() -> None:
    svc, job_repo, audit_repo = _make_service()
    failed_job = _make_job(status=JobStatus.FAILED)
    replayed_job = _make_job(status=JobStatus.PENDING, id=failed_job.id)
    job_repo.get_by_id.return_value = failed_job
    job_repo.update_status.return_value = replayed_job

    result = await svc.replay_job(failed_job.id, uuid.uuid4())

    job_repo.update_status.assert_awaited_once()
    audit_repo.log.assert_awaited_once()
    assert result is replayed_job


async def test_replay_non_failed_job_raises() -> None:
    svc, job_repo, _ = _make_service()
    running_job = _make_job(status=JobStatus.RUNNING)
    job_repo.get_by_id.return_value = running_job

    with pytest.raises(JobError, match="replayed"):
        await svc.replay_job(running_job.id, uuid.uuid4())
