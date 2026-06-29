"""Unit tests for JobService dependency handling."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.core.exceptions import NotFoundError
from app.models.enums import JobStatus, JobType
from app.models.job import Job
from app.services.job import JobService


def _job(**kw: object) -> MagicMock:
    j = MagicMock(spec=Job)
    j.id = kw.get("id", uuid.uuid4())
    j.user_id = kw.get("user_id", uuid.uuid4())
    j.status = kw.get("status", JobStatus.COMPLETED)
    j.type = kw.get("type", JobType.CSV_UPLOAD)
    j.trace_id = kw.get("trace_id", None)
    j.priority = kw.get("priority", 0)
    return j


def _make_service() -> tuple[JobService, AsyncMock, AsyncMock, AsyncMock, AsyncMock]:
    job_repo = AsyncMock()
    audit_repo = AsyncMock()
    outbox_repo = AsyncMock()
    dep_repo = AsyncMock()
    redis = AsyncMock()
    svc = JobService(job_repo, audit_repo, outbox_repo, redis, dep_repo=dep_repo)
    return svc, job_repo, audit_repo, outbox_repo, dep_repo


async def test_job_starts_pending_when_all_deps_already_completed() -> None:
    svc, job_repo, _, outbox_repo, dep_repo = _make_service()
    job_repo.get_by_idempotency_key.return_value = None
    parent = _job(status=JobStatus.COMPLETED)
    job_repo.get_by_id.return_value = parent
    new_job = _job(status=JobStatus.PENDING)
    job_repo.create.return_value = new_job

    result = await svc.create_job(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        job_type=JobType.CSV_UPLOAD,
        dependencies=[parent.id],
    )

    assert result is new_job
    create_kwargs = job_repo.create.await_args.kwargs
    assert create_kwargs["status"] == JobStatus.PENDING
    # Outbox publish happens for PENDING jobs.
    outbox_repo.add.assert_awaited_once()
    dep_repo.add.assert_awaited_once()


async def test_job_starts_waiting_when_a_parent_is_not_complete() -> None:
    svc, job_repo, _, outbox_repo, dep_repo = _make_service()
    job_repo.get_by_idempotency_key.return_value = None
    parent_done = _job(status=JobStatus.COMPLETED)
    parent_running = _job(status=JobStatus.RUNNING)

    async def get_by_id(jid: uuid.UUID) -> MagicMock:
        return parent_done if jid == parent_done.id else parent_running

    job_repo.get_by_id.side_effect = get_by_id
    new_job = _job(status=JobStatus.WAITING)
    job_repo.create.return_value = new_job

    await svc.create_job(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        job_type=JobType.CSV_UPLOAD,
        dependencies=[parent_done.id, parent_running.id],
    )

    create_kwargs = job_repo.create.await_args.kwargs
    assert create_kwargs["status"] == JobStatus.WAITING
    # No outbox publish — the dependency resolver will fire it later.
    outbox_repo.add.assert_not_awaited()
    dep_repo.add.assert_awaited_once()


async def test_missing_dependency_raises_not_found() -> None:
    svc, job_repo, *_ = _make_service()
    job_repo.get_by_idempotency_key.return_value = None
    job_repo.get_by_id.return_value = None

    with pytest.raises(NotFoundError):
        await svc.create_job(
            user_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            job_type=JobType.CSV_UPLOAD,
            dependencies=[uuid.uuid4()],
        )


async def test_no_dependencies_behaves_like_before() -> None:
    svc, job_repo, _, outbox_repo, dep_repo = _make_service()
    job_repo.get_by_idempotency_key.return_value = None
    new_job = _job(status=JobStatus.PENDING)
    job_repo.create.return_value = new_job

    await svc.create_job(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        job_type=JobType.CSV_UPLOAD,
    )

    dep_repo.add.assert_not_awaited()
    outbox_repo.add.assert_awaited_once()
