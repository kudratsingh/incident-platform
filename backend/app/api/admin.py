import uuid
from typing import Any

from app.dependencies import get_db, get_redis, require_role
from app.models.enums import UserRole
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.event_log import EventLogRepository
from app.repositories.job import JobRepository
from app.repositories.job_dependency import JobDependencyRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.tenant import TenantRepository
from app.repositories.triage import TriageRepository
from app.repositories.user import UserRepository
from app.schemas.common import PaginatedResponse
from app.schemas.job import AdminJobListParams, JobResponse
from app.schemas.user import UserResponse
from app.services.job import JobService
from app.services.runbooks import get as get_runbook
from app.services.runbooks import list_all as list_runbooks
from app.services.slo import compute_all as compute_slos
from app.utils.cache import JobCache
from app.workers.read_model import read_global_stats, read_user_stats
from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/admin", tags=["admin"])

_require_support_or_admin = require_role(UserRole.SUPPORT, UserRole.ADMIN)
_require_admin = require_role(UserRole.ADMIN)


def _job_service(db: AsyncSession, redis: Redis) -> JobService:
    return JobService(
        JobRepository(db),
        AuditRepository(db),
        OutboxRepository(db),
        redis,
        dep_repo=JobDependencyRepository(db),
    )


@router.get("/jobs", response_model=PaginatedResponse[JobResponse])
async def admin_list_jobs(
    params: AdminJobListParams = Depends(),
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> PaginatedResponse[JobResponse]:
    svc = _job_service(db, redis)
    jobs, total = await svc.list_jobs(
        requesting_user_id=current_user.id,
        user_role=current_user.role,
        tenant_id=current_user.tenant_id,
        page=params.page,
        page_size=params.page_size,
        status=params.status,
        job_type=params.type,
        trace_id=params.trace_id,
        filter_user_id=params.user_id,
    )
    return PaginatedResponse.build(
        items=[JobResponse.model_validate(j) for j in jobs],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def admin_get_job(
    job_id: uuid.UUID,
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> JobResponse:
    svc = _job_service(db, redis)
    job = await svc.get_job(
        job_id=job_id,
        requesting_user_id=current_user.id,
        user_role=current_user.role,
        tenant_id=current_user.tenant_id,
    )
    return JobResponse.model_validate(job)


@router.post("/jobs/{job_id}/replay", response_model=JobResponse)
async def replay_job(
    job_id: uuid.UUID,
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> JobResponse:
    svc = _job_service(db, redis)
    job = await svc.replay_job(
        job_id=job_id,
        requesting_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
    )
    await JobCache.delete(redis, job_id)
    return JobResponse.model_validate(job)


@router.get("/stats")
async def system_stats(
    current_user: User = Depends(_require_support_or_admin),
    redis: Redis = Depends(get_redis),
) -> dict[str, dict[str, int]]:
    """System-wide job counts by status, served from the CQRS read model.

    Reads denormalized Redis sets maintained by ReadModelProjector — no
    aggregate SQL on the jobs table. Numbers are eventually consistent
    with the write side (latency dominated by Kafka lag).
    """
    return {"by_status": await read_global_stats(redis)}


@router.get("/users/{user_id}/stats")
async def user_stats(
    user_id: uuid.UUID,
    current_user: User = Depends(_require_support_or_admin),
    redis: Redis = Depends(get_redis),
) -> dict[str, dict[str, int]]:
    """Per-user job counts by status, served from the CQRS read model."""
    return {"by_status": await read_user_stats(redis, str(user_id))}


@router.get("/slos")
async def list_slos(
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Current SLO state with error-budget and burn-rate per objective."""
    states = await compute_slos(db)
    return {
        "slos": [
            {
                "id": s.definition.id,
                "name": s.definition.name,
                "description": s.definition.description,
                "target": s.definition.target,
                "window_hours": s.definition.window_hours,
                "runbook_id": s.definition.runbook_id,
                "total": s.total,
                "failed": s.failed,
                "current": s.current,
                "budget_remaining_pct": s.budget_remaining_pct,
                "burn_rate": s.burn_rate if s.burn_rate != float("inf") else None,
                "healthy": s.healthy,
            }
            for s in states
        ]
    }


@router.get("/tenants")
async def admin_list_tenants(
    page: int = 1,
    page_size: int = 50,
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List every tenant in the system with per-tenant user + job counts.

    Admin-only. In a later PR this becomes "root-admin-only" once we have a
    way to designate platform operators vs per-tenant admins.
    """
    repo = TenantRepository(db)
    tenants, total = await repo.list_all(
        offset=(page - 1) * page_size, limit=page_size
    )
    items: list[dict[str, Any]] = []
    for t in tenants:
        counts = await repo.counts_for(t.id)
        items.append(
            {
                "id": str(t.id),
                "slug": t.slug,
                "name": t.name,
                "is_active": t.is_active,
                "created_at": t.created_at.isoformat(),
                "users": counts["users"],
                "jobs": counts["jobs"],
            }
        )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.post("/tenants", status_code=201)
async def admin_create_tenant(
    body: dict[str, Any],
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Create a new tenant. Body: {slug, name}."""
    from app.core.exceptions import ConflictError, RequestValidationError

    slug = (body.get("slug") or "").strip()
    name = (body.get("name") or "").strip()
    if not slug or not name:
        raise RequestValidationError("slug and name are required")
    if not slug.replace("-", "").replace("_", "").isalnum():
        raise RequestValidationError("slug may only contain alphanumerics, '-', '_'")

    repo = TenantRepository(db)
    if await repo.get_by_slug(slug) is not None:
        raise ConflictError(f"Tenant slug already exists: {slug}")
    tenant = await repo.create(slug=slug, name=name, is_active=True)
    return {
        "id": str(tenant.id),
        "slug": tenant.slug,
        "name": tenant.name,
        "is_active": tenant.is_active,
        "created_at": tenant.created_at.isoformat(),
    }


@router.get("/tenants/{tenant_id}")
async def admin_get_tenant(
    tenant_id: uuid.UUID,
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.core.exceptions import NotFoundError

    repo = TenantRepository(db)
    tenant = await repo.get_by_id(tenant_id)
    if tenant is None:
        raise NotFoundError(f"Tenant {tenant_id} not found")
    counts = await repo.counts_for(tenant.id)
    return {
        "id": str(tenant.id),
        "slug": tenant.slug,
        "name": tenant.name,
        "is_active": tenant.is_active,
        "created_at": tenant.created_at.isoformat(),
        "users": counts["users"],
        "jobs": counts["jobs"],
    }


@router.get("/runbooks")
async def admin_list_runbooks(
    current_user: User = Depends(_require_support_or_admin),
) -> dict[str, Any]:
    """All runbooks, ordered by id. Each one documents an alarm or SLO breach."""
    items = list_runbooks()
    return {"items": items, "count": len(items)}


@router.get("/runbooks/{runbook_id}")
async def admin_get_runbook(
    runbook_id: str,
    current_user: User = Depends(_require_support_or_admin),
) -> dict[str, Any]:
    from app.core.exceptions import NotFoundError

    rb = get_runbook(runbook_id)
    if rb is None:
        raise NotFoundError(f"Runbook {runbook_id} not found")
    return rb


@router.get("/dlq/stats")
async def dlq_stats(
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Counts of dead-lettered jobs for the admin DLQ badge / dashboard."""
    total, by_type = await JobRepository(db).dlq_stats(current_user.tenant_id)
    return {"total": total, "by_type": by_type}


@router.get("/jobs/{job_id}/triage")
async def job_triage(
    job_id: uuid.UUID,
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """LLM-generated triage analysis for a dead-lettered job.

    Returns 404 if no triage row exists (the job hasn't dead-lettered yet,
    triage is disabled, or the consumer hasn't caught up).
    """
    from app.core.exceptions import NotFoundError

    triage = await TriageRepository(db).get_by_job_id(job_id)
    if triage is None:
        raise NotFoundError(f"No triage analysis for job {job_id}")
    return {
        "id": str(triage.id),
        "job_id": str(triage.job_id),
        "root_cause_category": triage.root_cause_category,
        "summary": triage.summary,
        "suggested_fix": triage.suggested_fix,
        "is_retryable": triage.is_retryable,
        "confidence": triage.confidence,
        "model_used": triage.model_used,
        "usage": triage.usage,
        "created_at": triage.created_at.isoformat(),
        "updated_at": triage.updated_at.isoformat(),
    }


@router.get("/jobs/{job_id}/timeline")
async def job_timeline(
    job_id: uuid.UUID,
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Event-sourced timeline for a job — all Kafka lifecycle events in order.

    Replays the immutable job_events log, which the EventLogConsumer fills
    from every job.submitted / progress / completed / failed / dlq message.
    Useful for forensic / time-travel debugging of a single job's full
    history without trusting the mutable jobs row.
    """
    events = await EventLogRepository(db).timeline(job_id)
    return {
        "job_id": str(job_id),
        "count": len(events),
        "events": [
            {
                "id": str(e.id),
                "event_name": e.event_name,
                "recorded_at": e.recorded_at.isoformat(),
                "kafka_topic": e.kafka_topic,
                "kafka_partition": e.kafka_partition,
                "kafka_offset": e.kafka_offset,
                "payload": e.payload,
            }
            for e in events
        ],
    }


@router.post("/incidents/{job_id}/resolve", response_model=JobResponse)
async def resolve_incident(
    job_id: uuid.UUID,
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> JobResponse:
    svc = _job_service(db, redis)
    job = await svc.resolve_incident(
        job_id=job_id,
        requesting_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
    )
    await JobCache.delete(redis, job_id)
    return JobResponse.model_validate(job)


@router.get("/users", response_model=PaginatedResponse[UserResponse])
async def list_users(
    page: int = 1,
    page_size: int = 20,
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[UserResponse]:
    repo = UserRepository(db)
    users, total = await repo.list_all(offset=(page - 1) * page_size, limit=page_size)
    return PaginatedResponse.build(
        items=[UserResponse.model_validate(u) for u in users],
        total=total,
        page=page,
        page_size=page_size,
    )
