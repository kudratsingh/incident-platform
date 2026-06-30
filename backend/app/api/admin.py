import uuid
from typing import Any

from app.dependencies import (
    get_db,
    get_redis,
    require_platform_admin,
    require_role,
    resolve_admin_tenant,
)
from app.models.enums import UserRole
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.digest import DigestRepository
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
from app.services import incident_digest, nl_query
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
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> PaginatedResponse[JobResponse]:
    effective_tenant = await resolve_admin_tenant(current_user, db, tenant_id)
    svc = _job_service(db, redis)
    jobs, total = await svc.list_jobs(
        requesting_user_id=current_user.id,
        user_role=current_user.role,
        tenant_id=effective_tenant,
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


@router.post("/query")
async def admin_nl_query(
    body: dict[str, Any],
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Translate a plain-English question into a constrained job filter, apply
    it via the existing list_jobs path, and return both the spec and the
    rows. Body: `{"question": "..."}`.

    503 when the feature flag is off. The spec is a Pydantic model with
    enum/literal fields — the model can never smuggle raw SQL or unsafe
    field names into the query.
    """
    from app.core.exceptions import AppError, RequestValidationError

    class NLQueryUnavailable(AppError):
        status_code = 503
        error_code = "nl_query_unavailable"

    question = (body.get("question") or "").strip()
    if not question:
        raise RequestValidationError("question is required")
    if len(question) > 500:
        raise RequestValidationError("question is too long (max 500 chars)")

    if not nl_query.is_enabled():
        raise NLQueryUnavailable(
            "Natural-language queries are disabled. Set LLM_NL_QUERY_ENABLED=1."
        )

    try:
        spec, usage, model = await nl_query.parse_question(question)
    except nl_query.NLQueryDisabledError as exc:
        raise NLQueryUnavailable(str(exc)) from exc
    except Exception as exc:
        # Network blips / schema mismatches / timeouts shouldn't 500 — return
        # a 503 so the UI can show a friendly "try again" rather than a stack.
        raise NLQueryUnavailable(f"LLM call failed: {exc}") from exc

    effective_tenant = await resolve_admin_tenant(current_user, db, tenant_id)
    svc = _job_service(db, redis)
    jobs, total = await svc.list_jobs(
        requesting_user_id=current_user.id,
        user_role=current_user.role,
        tenant_id=effective_tenant,
        page=1,
        page_size=spec.limit,
        status=spec.status,
        job_type=spec.type,
        trace_id=spec.trace_id,
        created_after=spec.created_after,
        created_before=spec.created_before,
        retry_count_min=spec.retry_count_min,
        retry_count_max=spec.retry_count_max,
    )
    return {
        "spec": spec.model_dump(mode="json"),
        "model": model,
        "usage": usage,
        "items": [JobResponse.model_validate(j).model_dump(mode="json") for j in jobs],
        "total": total,
    }


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
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, dict[str, int]]:
    """System-wide job counts by status, served from the CQRS read model.

    Reads denormalized Redis sets maintained by ReadModelProjector — no
    aggregate SQL on the jobs table. Numbers are eventually consistent
    with the write side (latency dominated by Kafka lag).
    """
    # The CQRS read-model is keyed by tenant_id, so the override hits a
    # different Redis set; we don't have to touch app.tenant_id here.
    effective_tenant = await resolve_admin_tenant(current_user, db, tenant_id)
    return {"by_status": await read_global_stats(redis, str(effective_tenant))}


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


def _serialize_digest(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "window_start": row.window_start.isoformat(),
        "window_end": row.window_end.isoformat(),
        "summary": row.summary,
        "highlights": row.highlights or {},
        "model_used": row.model_used,
        "usage": row.usage or {},
        "created_at": row.created_at.isoformat(),
    }


@router.get("/digests")
async def admin_list_digests(
    tenant_id: uuid.UUID | None = None,
    limit: int = 20,
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Recent incident digests for the caller's tenant (or, for platform
    admins passing ?tenant_id=, the named tenant)."""
    effective_tenant = await resolve_admin_tenant(current_user, db, tenant_id)
    limit = max(1, min(100, limit))
    rows = await DigestRepository(db).list_for_tenant(effective_tenant, limit=limit)
    return {"items": [_serialize_digest(r) for r in rows], "count": len(rows)}


@router.post("/digests/generate", status_code=201)
async def admin_generate_digest(
    body: dict[str, Any] | None = None,
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Generate a digest for the caller's tenant immediately, without
    waiting for the periodic loop. Useful for incident-response time.

    503 if the feature flag is off. The window is the last
    `llm_digest_window_hours` (or `?hours=N` override, 1..168)."""
    from datetime import UTC, datetime, timedelta

    from app.core.exceptions import AppError

    class DigestUnavailable(AppError):
        status_code = 503
        error_code = "digest_unavailable"

    if not incident_digest.is_enabled():
        raise DigestUnavailable(
            "Incident digests are disabled. Set LLM_DIGEST_ENABLED=1."
        )

    from app.config import get_settings

    settings = get_settings()
    hours_raw = (body or {}).get("hours") if body else None
    try:
        hours = int(hours_raw) if hours_raw is not None else settings.llm_digest_window_hours
    except (TypeError, ValueError):
        hours = settings.llm_digest_window_hours
    hours = max(1, min(168, hours))

    effective_tenant = await resolve_admin_tenant(current_user, db, tenant_id)
    tenant_row = await TenantRepository(db).get_by_id(effective_tenant)
    if tenant_row is None:
        from app.core.exceptions import NotFoundError
        raise NotFoundError(f"Tenant {effective_tenant} not found")

    window_end = datetime.now(UTC)
    window_start = window_end - timedelta(hours=hours)
    try:
        row = await incident_digest.run_digest_for_tenant(
            db, tenant_row, window_start, window_end
        )
    except incident_digest.DigestDisabledError as exc:
        raise DigestUnavailable(str(exc)) from exc
    except Exception as exc:
        raise DigestUnavailable(f"LLM call failed: {exc}") from exc

    if row is None:
        # Empty window — no jobs to summarize. Return a 200-ish shape so the
        # UI can show "nothing happened" without a special error path.
        return {
            "summary": None,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
        }
    return _serialize_digest(row)


@router.get("/digests/{digest_id}")
async def admin_get_digest(
    digest_id: uuid.UUID,
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.core.exceptions import AuthorizationError, NotFoundError

    row = await DigestRepository(db).get_by_id(digest_id)
    if row is None:
        raise NotFoundError(f"Digest {digest_id} not found")
    # Tenant scope: a non-platform-admin can only read their own tenant's
    # digests. Platform admins can read any.
    if (
        not current_user.is_platform_admin
        and row.tenant_id != current_user.tenant_id
    ):
        raise AuthorizationError("Cross-tenant access denied")
    return _serialize_digest(row)


@router.get("/tenants")
async def admin_list_tenants(
    page: int = 1,
    page_size: int = 50,
    current_user: User = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List every tenant in the system with per-tenant user + job counts.

    Platform-admin-only. Tenant admins (`role=admin` without the platform
    flag) can't see sibling tenants; they're scoped to their own.
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
                "rate_limit_per_minute": t.rate_limit_per_minute,
                "quota_jobs_per_month": t.quota_jobs_per_month,
            }
        )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.post("/tenants", status_code=201)
async def admin_create_tenant(
    body: dict[str, Any],
    current_user: User = Depends(require_platform_admin),
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
    current_user: User = Depends(require_platform_admin),
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
        "rate_limit_per_minute": tenant.rate_limit_per_minute,
        "quota_jobs_per_month": tenant.quota_jobs_per_month,
    }


@router.patch("/tenants/{tenant_id}")
async def admin_update_tenant_limits(
    tenant_id: uuid.UUID,
    body: dict[str, Any],
    current_user: User = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Update a tenant's rate limit and/or monthly job quota.

    Body fields (both optional): `rate_limit_per_minute`, `quota_jobs_per_month`.
    Each must be a non-negative integer (0 disables the relevant check).
    """
    from app.core.exceptions import NotFoundError, RequestValidationError

    repo = TenantRepository(db)
    tenant = await repo.get_by_id(tenant_id)
    if tenant is None:
        raise NotFoundError(f"Tenant {tenant_id} not found")

    changed = False
    for field in ("rate_limit_per_minute", "quota_jobs_per_month"):
        if field in body:
            value = body[field]
            if not isinstance(value, int) or value < 0:
                raise RequestValidationError(f"{field} must be a non-negative integer")
            setattr(tenant, field, value)
            changed = True
    if changed:
        await db.flush()

    return {
        "id": str(tenant.id),
        "slug": tenant.slug,
        "name": tenant.name,
        "is_active": tenant.is_active,
        "rate_limit_per_minute": tenant.rate_limit_per_minute,
        "quota_jobs_per_month": tenant.quota_jobs_per_month,
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
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Counts of dead-lettered jobs for the admin DLQ badge / dashboard."""
    effective_tenant = await resolve_admin_tenant(current_user, db, tenant_id)
    total, by_type = await JobRepository(db).dlq_stats(effective_tenant)
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
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[UserResponse]:
    effective_tenant = await resolve_admin_tenant(current_user, db, tenant_id)
    repo = UserRepository(db)
    users, total = await repo.list_all(
        offset=(page - 1) * page_size,
        limit=page_size,
        tenant_id=effective_tenant,
    )
    return PaginatedResponse.build(
        items=[UserResponse.model_validate(u) for u in users],
        total=total,
        page=page,
        page_size=page_size,
    )
