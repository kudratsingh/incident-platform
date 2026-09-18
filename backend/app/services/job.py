"""Job service — the single place a job is created, read, replayed or resolved.

Sits between the API routers, the admin console and the MCP action tools on one
side and the repositories on the other; the job row, its audit row and its
outbox event are always written together.
"""

import uuid
from functools import partial
from typing import Any

from app.config import get_settings
from app.core.exceptions import AuthorizationError, JobError, NotFoundError
from app.core.logging import get_logger, request_id_var, trace_id_var
from app.core.tracing import inject_context
from app.models.enums import JobStatus, UserRole
from app.models.job import Job, _default_max_attempts
from app.repositories.audit import AuditRepository
from app.repositories.job import JobRepository
from app.repositories.job_dependency import JobDependencyRepository
from app.repositories.outbox import OutboxRepository
from app.utils.cache import JobCache
from app.utils.dag_pause import find_blocking_pause
from app.utils.post_commit import register_post_commit
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError

logger = get_logger(__name__)


class JobService:
    """Job operations every caller shares, so the rules around them — tenant
    scope, audit, cache invalidation — cannot differ by entry point."""

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
        tenant_id: uuid.UUID,
        job_type: str,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        priority: int = 0,
        max_attempts: int | None = None,
        dependencies: list[uuid.UUID] | None = None,
        saga_id: uuid.UUID | None = None,
        saga_step_index: int | None = None,
    ) -> Job:
        """Create a job, or hand back the one an earlier call with the same
        idempotency key already made.

        A job whose parents are unfinished — or whose chain is paused — starts
        WAITING and is announced later; anything else is announced now.
        """
        # `None` means the `MAX_JOB_ATTEMPTS` default, not a literal 3 (WO-R2-76).
        if max_attempts is None:
            max_attempts = _default_max_attempts()

        # Idempotency: return the existing job if this key was already used
        # in this tenant. (Different tenants can reuse the same key.)
        if idempotency_key:
            existing = await self.job_repo.get_by_idempotency_key(
                idempotency_key, tenant_id
            )
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

        # E1-08: all-COMPLETED parents used to dispatch immediately even onto a paused
        # chain. Hold WAITING; `_resume_unblocked_waiting_loop` promotes it on unpause.
        if initial_status == JobStatus.PENDING and deps and self.dep_repo is not None:
            for parent in parent_jobs:
                paused_by = await find_blocking_pause(
                    self.redis, self.dep_repo, parent.id
                )
                if paused_by is not None:
                    logger.info(
                        "job created into a paused DAG — held waiting",
                        extra={
                            "parent_id": str(parent.id),
                            "paused_by": str(paused_by),
                        },
                    )
                    initial_status = JobStatus.WAITING
                    break

        # Carry the current OTel span context through the queue boundary so the
        # worker can create a proper child span linked to this request's trace.
        otel_ctx = inject_context()
        enriched_payload: dict[str, Any] = {**(payload or {})}
        if otel_ctx:
            enriched_payload["__traceparent"] = otel_ctx

        # Guards the check-then-insert race: the composite UNIQUE on
        # (tenant_id, idempotency_key) rejects the loser, so roll back only the
        # savepoint and re-fetch — a hit is the winner's row, a miss is some
        # other constraint and surfaces.
        session = self.job_repo.session
        try:
            async with session.begin_nested():
                job = await self.job_repo.create(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    type=job_type,
                    status=initial_status,
                    idempotency_key=idempotency_key,
                    payload=enriched_payload,
                    priority=priority,
                    max_attempts=max_attempts,
                    trace_id=trace_id_var.get("") or None,
                    saga_id=saga_id,
                    saga_step_index=saga_step_index,
                )
        except IntegrityError:
            if idempotency_key is None:
                raise
            winner = await self.job_repo.get_by_idempotency_key(
                idempotency_key, tenant_id
            )
            if winner is None:
                # Not the idempotency race — some other constraint failed.
                raise
            logger.info(
                "idempotency race resolved — returning winner",
                extra={
                    "idempotency_key": idempotency_key,
                    "job_id": str(winner.id),
                    "tenant_id": str(tenant_id),
                },
            )
            return winner

        if deps:
            if self.dep_repo is None:
                raise RuntimeError(
                    "JobService.create_job called with dependencies but no dep_repo"
                )
            await self.dep_repo.add(job.id, deps)

        await self.audit_repo.log(
            "job.created",
            tenant_id=tenant_id,
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
                tenant_id=tenant_id,
                topic=settings.kafka_topic_job_submitted,
                key=f"{tenant_id}:{user_id}",
                payload={
                    "event": "job.submitted",
                    "tenant_id": str(tenant_id),
                    "job_id": str(job.id),
                    "user_id": str(user_id),
                    "job_type": job_type,
                    "payload": enriched_payload,
                    "priority": priority,
                    "trace_id": job.trace_id,
                },
            )

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
        self,
        job_id: uuid.UUID,
        requesting_user_id: uuid.UUID,
        user_role: str,
        tenant_id: uuid.UUID,
    ) -> Job:
        """One job from the caller's own tenant. Someone else's job is refused
        unless the caller is support or admin."""
        # Tenant scope first — a cross-tenant lookup is a 404, never an
        # AuthorizationError, so the caller can't even infer the row exists.
        job = await self.job_repo.get_for_tenant(job_id, tenant_id)
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
        tenant_id: uuid.UUID,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
        job_type: str | None = None,
        trace_id: str | None = None,
        filter_user_id: uuid.UUID | None = None,
        created_after: Any = None,
        created_before: Any = None,
        retry_count_min: int | None = None,
        retry_count_max: int | None = None,
    ) -> tuple[list[Job], int]:
        """A filtered page of jobs plus the total. Ordinary users only ever see
        their own; support and admin see the whole tenant."""
        # Non-admins can only see their own jobs
        effective_user_id: uuid.UUID | None
        if user_role in (UserRole.ADMIN, UserRole.SUPPORT):
            effective_user_id = filter_user_id
        else:
            effective_user_id = requesting_user_id

        return await self.job_repo.list_jobs(
            tenant_id=tenant_id,
            offset=(page - 1) * page_size,
            limit=page_size,
            user_id=effective_user_id,
            status=status,
            job_type=job_type,
            trace_id=trace_id,
            created_after=created_after,
            created_before=created_before,
            retry_count_min=retry_count_min,
            retry_count_max=retry_count_max,
        )

    async def replay_job(
        self,
        job_id: uuid.UUID,
        tenant_id: uuid.UUID,
        *,
        requesting_user_id: uuid.UUID | None = None,
        principal_type: str = "user",
        principal_id: uuid.UUID | None = None,
    ) -> Job:
        """Replay a failed/dead-letter job.

        Human callers pass `requesting_user_id`; machine callers pass `principal_type` +
        `principal_id`, because an SA id in `audit_logs.user_id` violates the users FK.
        """
        job = await self.job_repo.get_for_tenant(job_id, tenant_id)
        if not job:
            raise NotFoundError(f"Job {job_id} not found")
        if job.status not in (JobStatus.FAILED, JobStatus.DEAD_LETTER):
            raise JobError(f"Only failed/dead_letter jobs can be replayed, got: {job.status}")

        # E1-08: a replay is a NEW dispatch, so it must respect the pause. This is the
        # single choke point for the admin endpoint, the three MCP replay tools and the
        # scheduled DLQ-replay loop, and the refusal must precede every mutation below.
        if self.dep_repo is not None:
            paused_by = await find_blocking_pause(self.redis, self.dep_repo, job_id)
            if paused_by is not None:
                raise JobError(
                    f"Job {job_id} is in a paused DAG (paused via {paused_by}); "
                    "replay refused while the pause holds"
                )

        # Snapshot before the write (R2-23): `update_status` writes through this same
        # identity-mapped `job`, so a later read of `job.status` returns "pending".
        previous_retry_count = job.retry_count
        previous_status = job.status

        # Reset retry_count, or the job dead-letters again on its first failure.
        updated = await self.job_repo.update_status(
            job_id,
            JobStatus.PENDING,
            extra={
                "retry_count": 0,
                "error_message": None,
                "result": None,
                # Clear the previous run's DLQ attribution — a replay is a
                # fresh lifecycle (F2-16).
                "dead_lettered_by": None,
                # And the remediation category with it (R2-23): it is scoped to
                # one dead-letter episode, so a surviving one would route every
                # later, unrelated dead-letter of this job. That is also what
                # stops the bulk replay's skip of it (R2-22) being permanent.
                "remediation_hint": None,
                # And the fence stamps for that category (WO-R2-158) — a
                # `fenced_at` beside a NULL hint describes a classification
                # that no longer exists.
                "fenced_at": None,
                "fenced_by": None,
            },
        )
        audit_user_id: uuid.UUID | None
        if principal_type == "user":
            audit_user_id = requesting_user_id
        else:
            # SA path — user_id must be NULL (FK to users), machine id
            # goes in principal_id where it doesn't have a FK.
            audit_user_id = None
        await self.audit_repo.log(
            "job.replayed",
            tenant_id=job.tenant_id,
            user_id=audit_user_id,
            principal_type=principal_type,
            principal_id=principal_id if principal_id is not None else requesting_user_id,
            job_id=job_id,
            resource_type="job",
            resource_id=str(job_id),
            request_id=request_id_var.get("") or None,
            extra_data={
                "previous_status": previous_status,
                "previous_retry_count": previous_retry_count,
            },
        )
        settings = get_settings()
        await self.outbox_repo.add(
            tenant_id=job.tenant_id,
            topic=settings.kafka_topic_job_submitted,
            key=f"{job.tenant_id}:{job.user_id}",
            payload={
                "event": "job.submitted",
                "tenant_id": str(job.tenant_id),
                "job_id": str(job_id),
                "user_id": str(job.user_id),
                "job_type": job.type,
                "payload": dict(job.payload or {}),
                "priority": job.priority,
                "trace_id": job.trace_id,
            },
        )
        # Same snapshots as the audit row, for the same reason (R2-23) — both
        # fields read post-write here too.
        logger.info(
            "job.replayed",
            extra={
                "job_id": str(job_id),
                "previous_status": previous_status,
                "previous_retry_count": previous_retry_count,
                "replayed_by": str(
                    principal_id if principal_id is not None else requesting_user_id
                ),
                "principal_type": principal_type,
            },
        )
        # Invalidate in the service, not the REST wrapper (E2-02): this is the single
        # choke point every replay path goes through. Deferred to post-commit (R2-23) —
        # evicting before the commit invites a reader to cache the unchanged row behind
        # it; `JobCache.invalidate` closes the slot as well as clearing it.
        register_post_commit(
            self.job_repo.session,
            partial(JobCache.invalidate, self.redis, job_id, tenant_id),
        )
        assert updated is not None
        return updated

    async def resolve_incident(
        self, job_id: uuid.UUID, requesting_user_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> Job:
        """Close a job out by hand: mark it completed and record who did it."""
        job = await self.job_repo.get_for_tenant(job_id, tenant_id)
        if not job:
            raise NotFoundError(f"Job {job_id} not found")

        updated = await self.job_repo.update_status(job_id, JobStatus.COMPLETED)
        # Same invariant as replay_job: the service layer owns invalidation,
        # so every caller of resolve_incident is coherent (E2-02).
        await JobCache.delete(self.redis, job_id, tenant_id)
        await self.audit_repo.log(
            "incident.resolved",
            tenant_id=job.tenant_id,
            user_id=requesting_user_id,
            job_id=job_id,
            resource_type="job",
            resource_id=str(job_id),
            request_id=request_id_var.get("") or None,
        )
        logger.info("incident resolved", extra={"job_id": str(job_id)})
        assert updated is not None
        return updated
