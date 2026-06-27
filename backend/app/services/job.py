import uuid
from typing import Any

from app.config import get_settings
from app.core.exceptions import AuthorizationError, JobError, NotFoundError
from app.core.logging import get_logger, request_id_var, trace_id_var
from app.core.tracing import inject_context
from app.models.enums import JobStatus, UserRole
from app.models.job import Job
from app.repositories.audit import AuditRepository
from app.repositories.job import JobRepository
from app.repositories.job_dependency import JobDependencyRepository
from app.repositories.outbox import OutboxRepository
from app.workers import queue
from redis.asyncio import Redis

logger = get_logger(__name__)


class JobService:
    def __init__(
        self,
        job_repo: JobRepository,
        audit_repo: AuditRepository,
        outbox_repo: OutboxRepository,
        redis: Redis,
        dep_repo: JobDependencyRepository | None = None,
    ) -> None:
        self.job_repo = job_repo
        self.audit_repo = audit_repo
        self.outbox_repo = outbox_repo
        self.redis = redis
        self.dep_repo = dep_repo

    async def create_job(
        self,
        user_id: uuid.UUID,
        job_type: str,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        priority: int = 0,
        max_retries: int = 3,
        dependencies: list[uuid.UUID] | None = None,
        saga_id: uuid.UUID | None = None,
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

        # Validate any declared dependencies exist before we open the tx for
        # the new job, so we can surface a clean 404 rather than an FK error.
        deps = dependencies or []
        parent_jobs: list[Job] = []
        for parent_id in deps:
            parent = await self.job_repo.get_by_id(parent_id)
            if parent is None:
                raise NotFoundError(f"Dependency job {parent_id} not found")
            parent_jobs.append(parent)

        # A job is WAITING iff any declared parent is not yet COMPLETED.
        # Cycles are impossible: new ids can't appear among existing parents.
        has_unmet = any(p.status != JobStatus.COMPLETED for p in parent_jobs)
        initial_status = JobStatus.WAITING if has_unmet else JobStatus.PENDING

        # Carry the current OTel span context through the queue boundary so the
        # worker can create a proper child span linked to this request's trace.
        otel_ctx = inject_context()
        enriched_payload: dict[str, Any] = {**(payload or {})}
        if otel_ctx:
            enriched_payload["__traceparent"] = otel_ctx

        job = await self.job_repo.create(
            user_id=user_id,
            type=job_type,
            status=initial_status,
            idempotency_key=idempotency_key,
            payload=enriched_payload,
            priority=priority,
            max_retries=max_retries,
            trace_id=trace_id_var.get("") or None,
            saga_id=saga_id,
        )

        if deps:
            if self.dep_repo is None:
                raise RuntimeError(
                    "JobService.create_job called with dependencies but no dep_repo"
                )
            await self.dep_repo.add(job.id, deps)

        await self.audit_repo.log(
            "job.created",
            user_id=user_id,
            job_id=job.id,
            resource_type="job",
            resource_id=str(job.id),
            request_id=request_id_var.get("") or None,
            extra_data={
                "type": job_type,
                "priority": priority,
                "dependencies": [str(d) for d in deps],
                "initial_status": initial_status,
            },
        )

        # Only publish job.submitted when the job is actually ready to run.
        # WAITING jobs are activated by DependencyResolver once parents complete.
        if initial_status == JobStatus.PENDING:
            settings = get_settings()
            await self.outbox_repo.add(
                topic=settings.kafka_topic_job_submitted,
                key=str(user_id),
                payload={
                    "event": "job.submitted",
                    "job_id": str(job.id),
                    "user_id": str(user_id),
                    "job_type": job_type,
                    "payload": enriched_payload,
                    "priority": priority,
                    "trace_id": job.trace_id,
                },
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
                "status": initial_status,
                "dependency_count": len(deps),
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

        # Reset retry_count so a DLQ replay actually gets fresh retries.
        # Without this, a job at retry_count==max_retries would dead-letter
        # again on the first failure of its replayed run.
        previous_retry_count = job.retry_count
        updated = await self.job_repo.update_status(
            job_id,
            JobStatus.PENDING,
            extra={"retry_count": 0, "error_message": None, "result": None},
        )
        await self.audit_repo.log(
            "job.replayed",
            user_id=requesting_user_id,
            job_id=job_id,
            resource_type="job",
            resource_id=str(job_id),
            request_id=request_id_var.get("") or None,
            extra_data={
                "previous_status": job.status,
                "previous_retry_count": previous_retry_count,
            },
        )
        settings = get_settings()
        await self.outbox_repo.add(
            topic=settings.kafka_topic_job_submitted,
            key=str(job.user_id),
            payload={
                "event": "job.submitted",
                "job_id": str(job_id),
                "user_id": str(job.user_id),
                "job_type": job.type,
                "payload": dict(job.payload or {}),
                "priority": job.priority,
                "trace_id": job.trace_id,
            },
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
