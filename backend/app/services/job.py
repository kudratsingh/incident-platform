import uuid
from typing import Any

from app.core.exceptions import AuthorizationError, JobError, NotFoundError
from app.core.logging import get_logger, request_id_var, trace_id_var
from app.models.enums import JobStatus, UserRole
from app.models.job import Job
from app.repositories.audit import AuditRepository
from app.repositories.job import JobRepository
from app.workers import queue
from redis.asyncio import Redis

logger = get_logger(__name__)


class JobService:
    def __init__(
        self, job_repo: JobRepository, audit_repo: AuditRepository, redis: Redis
    ) -> None:
        self.job_repo = job_repo
        self.audit_repo = audit_repo
        self.redis = redis

    async def create_job(
        self,
        user_id: uuid.UUID,
        job_type: str,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        priority: int = 0,
        max_retries: int = 3,
    ) -> Job:
        # Idempotency: return the existing job if this key was already used
        if idempotency_key:
            existing = await self.job_repo.get_by_idempotency_key(idempotency_key)
            if existing:
                logger.info(
                    "idempotent job returned",
                    extra={"idempotency_key": idempotency_key, "job_id": str(existing.id)},
                )
                return existing

        job = await self.job_repo.create(
            user_id=user_id,
            type=job_type,
            status=JobStatus.PENDING,
            idempotency_key=idempotency_key,
            payload=payload,
            priority=priority,
            max_retries=max_retries,
            trace_id=trace_id_var.get("") or None,
        )
        await self.audit_repo.log(
            "job.created",
            user_id=user_id,
            job_id=job.id,
            resource_type="job",
            resource_id=str(job.id),
            request_id=request_id_var.get("") or None,
            extra_data={"type": job_type, "priority": priority},
        )
        await queue.push(self.redis, str(job.id), priority=priority)
        logger.info(
            "job.created",
            extra={
                "job_id": str(job.id),
                "type": job_type,
                "priority": priority,
                "trace_id": str(job.trace_id),
                "user_id": str(user_id),
            },
        )
        return job

    async def get_job(
        self, job_id: uuid.UUID, requesting_user_id: uuid.UUID, user_role: str
    ) -> Job:
        job = await self.job_repo.get_by_id(job_id)
        if not job:
            raise NotFoundError(f"Job {job_id} not found")
        privileged = user_role in (UserRole.ADMIN, UserRole.SUPPORT)
        if not privileged and job.user_id != requesting_user_id:
            raise AuthorizationError("Not allowed to view this job")
        return job

    async def list_jobs(
        self,
        requesting_user_id: uuid.UUID,
        user_role: str,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
        job_type: str | None = None,
        trace_id: str | None = None,
        filter_user_id: uuid.UUID | None = None,
    ) -> tuple[list[Job], int]:
        # Non-admins can only see their own jobs
        effective_user_id: uuid.UUID | None
        if user_role in (UserRole.ADMIN, UserRole.SUPPORT):
            effective_user_id = filter_user_id
        else:
            effective_user_id = requesting_user_id

        return await self.job_repo.list_jobs(
            offset=(page - 1) * page_size,
            limit=page_size,
            user_id=effective_user_id,
            status=status,
            job_type=job_type,
            trace_id=trace_id,
        )

    async def replay_job(
        self, job_id: uuid.UUID, requesting_user_id: uuid.UUID
    ) -> Job:
        job = await self.job_repo.get_by_id(job_id)
        if not job:
            raise NotFoundError(f"Job {job_id} not found")
        if job.status not in (JobStatus.FAILED, JobStatus.DEAD_LETTER):
            raise JobError(f"Only failed/dead_letter jobs can be replayed, got: {job.status}")

        updated = await self.job_repo.update_status(
            job_id,
            JobStatus.PENDING,
            extra={"retry_count": job.retry_count, "error_message": None, "result": None},
        )
        await self.audit_repo.log(
            "job.replayed",
            user_id=requesting_user_id,
            job_id=job_id,
            resource_type="job",
            resource_id=str(job_id),
            request_id=request_id_var.get("") or None,
        )
        await queue.push(self.redis, str(job_id), priority=0)
        logger.info(
            "job.replayed",
            extra={
                "job_id": str(job_id),
                "previous_status": job.status,
                "retry_count": job.retry_count,
                "replayed_by": str(requesting_user_id),
            },
        )
        assert updated is not None
        return updated

    async def resolve_incident(
        self, job_id: uuid.UUID, requesting_user_id: uuid.UUID
    ) -> Job:
        job = await self.job_repo.get_by_id(job_id)
        if not job:
            raise NotFoundError(f"Job {job_id} not found")

        updated = await self.job_repo.update_status(job_id, JobStatus.COMPLETED)
        await self.audit_repo.log(
            "incident.resolved",
            user_id=requesting_user_id,
            job_id=job_id,
            resource_type="job",
            resource_id=str(job_id),
            request_id=request_id_var.get("") or None,
        )
        logger.info("incident resolved", extra={"job_id": str(job_id)})
        assert updated is not None
        return updated
