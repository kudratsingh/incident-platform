"""The operator console's HTTP surface: jobs and the DLQ, replay and resolve,
SLOs and runbooks, timelines, tenants, users and audit. The handlers are thin:
roles are a dependency and the work belongs to the services.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.breaker_state import read_breaker_states
from app.core.consumer_lag import (
    LAG_SAMPLES_KEEP,
    LAG_SAMPLES_WINDOW_SECONDS,
    LIVE_REFRESHED_GROUP,
    SEEDED_CONSUMER_GROUPS,
    LagReading,
    read_lag,
)
from app.core.logging import request_id_var
from app.dependencies import (
    get_db,
    get_effective_tenant,
    get_redis,
    get_session_factory,
    require_platform_admin,
    require_role,
    resolve_admin_tenant,
)
from app.models.enums import JobStatus, UserRole
from app.models.user import User
from app.repositories.agent_run import AgentRunRepository
from app.repositories.alert import AlertRepository
from app.repositories.audit import AuditRepository
from app.repositories.digest import DigestRepository
from app.repositories.event_log import EventLogRepository
from app.repositories.job import JobRepository
from app.repositories.job_dependency import JobDependencyRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.tenant import TenantRepository
from app.repositories.triage import TriageRepository
from app.repositories.user import UserRepository
from app.schemas.agent_run import (
    AgentRunListParams,
    AgentRunResponse,
    AgentRunStepResponse,
    AgentRunStepsResponse,
    AgentRunSummaryResponse,
    AlertListParams,
    AlertResponse,
    CircuitBreakerResponse,
    CircuitBreakersResponse,
    ConsumerLagGroupResponse,
    ConsumerLagResponse,
    ConsumerLagSampleResponse,
)
from app.schemas.common import MAX_PAGE_SIZE, PaginatedResponse
from app.schemas.job import AdminJobListParams, JobResponse, JobTriageSummary
from app.schemas.tenant import TenantLimitsUpdate
from app.schemas.user import UserResponse
from app.services import incident_digest, nl_query
from app.services.job import JobService
from app.services.runbooks import get as get_runbook
from app.services.runbooks import list_all as list_runbooks
from app.services.slo import compute_all as compute_slos
from app.utils.rate_limit import check_identity_rate_limit
from app.workers.read_model import read_global_stats, read_user_stats
from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Separate Redis buckets so exhausting the digest allowance never blocks a
# natural-language query.
ADMIN_NL_QUERY_RATE_BUCKET = "admin:nl_query"
ADMIN_DIGEST_RATE_BUCKET = "admin:digest"

router = APIRouter(prefix="/admin", tags=["admin"])

_require_support_or_admin = require_role(UserRole.SUPPORT, UserRole.ADMIN)
_require_admin = require_role(UserRole.ADMIN)


async def _set_rls_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Point this transaction's `app.tenant_id` at `tenant_id`.

    The tenant-management writes below need it: their audit row belongs to the
    tenant acted on, and `audit_logs` has a WITH CHECK on `tenant_id` under
    FORCE RLS (migration a7e3d9c41f28, ADR 0015). No-op on SQLite.
    """
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        await db.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )


def _job_service(db: AsyncSession, redis: Redis) -> JobService:
    return JobService(
        JobRepository(db),
        AuditRepository(db),
        OutboxRepository(db),
        redis,
        dep_repo=JobDependencyRepository(db),
    )


async def _with_triage(db: AsyncSession, jobs: list[Any]) -> list[JobResponse]:
    """Serialize a page of jobs, attaching the triage summary to dead-lettered rows.

    One statement for the page, not one per row (WO-R3-312): the DLQ tab and the demo
    console both poll this list. A job with no triage row keeps `triage: null`, which
    is the normal case — the LLM triage consumer is off by default.
    """
    dlq_ids = [j.id for j in jobs if j.status == JobStatus.DEAD_LETTER.value]
    triages = await TriageRepository(db).map_by_job_ids(dlq_ids)
    items: list[JobResponse] = []
    for job in jobs:
        response = JobResponse.model_validate(job)
        row = triages.get(job.id)
        if row is not None:
            response.triage = JobTriageSummary.model_validate(row)
        items.append(response)
    return items


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
        items=await _with_triage(db, list(jobs)),
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
    """Translate a plain-English question into a constrained job filter and run
    it through `list_jobs`. Body: `{"question": "..."}`.

    503 when the flag is off; the spec's enum/literal fields cannot carry SQL.
    """
    from app.config import get_settings
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

    # Immediately before the paid call, not earlier: this bucket counts
    # Anthropic calls (~$0.006 each), so an empty question or a 503 from the
    # flag must not spend an operator's allowance (WO-R2-30). Keyed on the
    # admin user, not the client IP. Fails open on a Redis error.
    await check_identity_rate_limit(
        redis,
        identity=current_user.id,
        limit=get_settings().admin_nl_query_rate_limit,
        window=get_settings().admin_paid_rate_limit_window_seconds,
        bucket=ADMIN_NL_QUERY_RATE_BUCKET,
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
    return (await _with_triage(db, [job]))[0]


@router.post("/jobs/{job_id}/replay", response_model=JobResponse)
async def replay_job(
    job_id: uuid.UUID,
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> JobResponse:
    svc = _job_service(db, redis)
    # JobService owns cache invalidation (E2-02) — no delete here.
    job = await svc.replay_job(
        job_id=job_id,
        requesting_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
    )
    return JobResponse.model_validate(job)


@router.get("/stats")
async def system_stats(
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, dict[str, int]]:
    """System-wide job counts by status, from the CQRS read model (Redis sets
    kept by ReadModelProjector; eventually consistent)."""
    # The CQRS read-model is keyed by tenant_id, so the override hits a
    # different Redis set; we don't have to touch app.tenant_id here.
    effective_tenant = await resolve_admin_tenant(current_user, db, tenant_id)
    return {"by_status": await read_global_stats(redis, str(effective_tenant))}


@router.get("/users/{user_id}/stats")
async def user_stats(
    user_id: uuid.UUID,
    current_user: User = Depends(_require_support_or_admin),
    effective_tenant: uuid.UUID = Depends(get_effective_tenant),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, dict[str, int]]:
    """Per-user job counts by status, from the CQRS read model.

    The user is resolved in Postgres under the caller's effective tenant
    *before* the cache is read — Redis has no RLS backstop, so a cache read
    must never be the authorisation boundary (WO-R2-50). 404 either way.
    """
    from app.core.exceptions import NotFoundError

    target = await UserRepository(db).get_for_tenant(user_id, effective_tenant)
    if target is None:
        raise NotFoundError(f"User {user_id} not found")
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
    redis: Redis = Depends(get_redis),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> dict[str, Any]:
    """Generate a digest for the caller's tenant now, not waiting for the
    periodic loop.

    503 if the flag is off. Window is `llm_digest_window_hours`, or `?hours=N`
    (1..168). The digest runs on `session_factory`'s own short transactions."""
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

    # Same position as POST /query: immediately before the paid call. Tighter
    # ceiling — a digest costs more (~$0.018) and `_digest_loop` already runs
    # on a schedule.
    await check_identity_rate_limit(
        redis,
        identity=current_user.id,
        limit=settings.admin_digest_rate_limit,
        window=settings.admin_paid_rate_limit_window_seconds,
        bucket=ADMIN_DIGEST_RATE_BUCKET,
    )

    window_end = datetime.now(UTC)
    window_start = window_end - timedelta(hours=hours)

    # Three phases on their own transactions, the Anthropic round-trip holding
    # none (WO-R2-127). Each re-issues `set_config('app.tenant_id')` because
    # the setting is TRANSACTION-LOCAL, and not re-issuing it fails silently:
    # the `tenant_isolation` bootstrap branch admits an unset value, so the
    # statement runs with no isolation (test_rls_enforcement.py proves both).
    try:
        async with session_factory() as read_session:
            async with read_session.begin():
                await _set_rls_tenant(read_session, effective_tenant)
                stats = await incident_digest.collect_window_stats(
                    read_session, tenant_row, window_start, window_end
                )

        if stats is None:
            # Empty window: nothing to summarize, so no paid call and a 200
            # shape the UI needs no error path for.
            return {
                "summary": None,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
            }
        by_status, failed_by_type, errors = stats

        digest_obj, usage, model = await incident_digest.generate_digest(
            tenant_slug=tenant_row.slug,
            window_start=window_start,
            window_end=window_end,
            by_status_count=by_status,
            by_type_failed_count=failed_by_type,
            error_messages=errors,
        )

        async with session_factory() as write_session:
            async with write_session.begin():
                await _set_rls_tenant(write_session, effective_tenant)
                row = await incident_digest.persist_digest(
                    write_session,
                    tenant_row,
                    window_start,
                    window_end,
                    by_status,
                    failed_by_type,
                    digest_obj,
                    usage,
                    model,
                )
    except incident_digest.DigestDisabledError as exc:
        raise DigestUnavailable(str(exc)) from exc
    except Exception as exc:
        raise DigestUnavailable(f"LLM call failed: {exc}") from exc

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
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    current_user: User = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Every tenant with per-tenant user + job counts. Platform-admin only —
    `role=admin` alone cannot see sibling tenants."""
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

    # The most privileged operator action here gets an audit row (F1-08),
    # and it belongs to the NEW tenant — hence the retarget around the write,
    # not in a `finally` that would mask a failed INSERT.
    await _set_rls_tenant(db, tenant.id)
    await AuditRepository(db).log(
        "tenant.created",
        tenant_id=tenant.id,
        user_id=current_user.id,
        resource_type="tenant",
        resource_id=str(tenant.id),
        request_id=request_id_var.get("") or None,
        extra_data={"slug": tenant.slug, "name": tenant.name},
    )
    await _set_rls_tenant(db, current_user.tenant_id)

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
    body: TenantLimitsUpdate,
    current_user: User = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Update a tenant's rate limit and/or monthly job quota.

    Both optional; 0 disables that check. The bounds live on
    `TenantLimitsUpdate`, not here — the `isinstance(value, int)` it replaced
    took a JSON `true` as a rate limit of 1 (WO-R2-61).
    """
    from app.core.exceptions import NotFoundError

    repo = TenantRepository(db)
    tenant = await repo.get_by_id(tenant_id)
    if tenant is None:
        raise NotFoundError(f"Tenant {tenant_id} not found")

    fields = ("rate_limit_per_minute", "quota_jobs_per_month")
    before = {f: getattr(tenant, f) for f in fields}
    # `exclude_unset` keeps this a genuine partial update: a field the
    # caller omitted is left alone rather than reset to a default.
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(tenant, field, value)
    after = {f: getattr(tenant, f) for f in fields}
    changed = after != before
    if changed:
        await db.flush()
        # Who loosened which limit, and from what (F1-08). Audited under the
        # TARGET tenant — see `_set_rls_tenant`. No change, no row.
        await _set_rls_tenant(db, tenant_id)
        await AuditRepository(db).log(
            "tenant.limits_updated",
            tenant_id=tenant_id,
            user_id=current_user.id,
            resource_type="tenant",
            resource_id=str(tenant_id),
            request_id=request_id_var.get("") or None,
            extra_data={"before": before, "after": after},
        )
        await _set_rls_tenant(db, current_user.tenant_id)

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
    """LLM triage analysis for a dead-lettered job. 404 when no row exists."""
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
    """Event-sourced timeline for a job — every Kafka lifecycle event in order,
    replayed from the immutable job_events log rather than the mutable jobs
    row."""
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
    # JobService owns cache invalidation (E2-02) — no delete here.
    job = await svc.resolve_incident(
        job_id=job_id,
        requesting_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
    )
    return JobResponse.model_validate(job)


# ---------------------------------------------------------------------------
# The demo console's reads (WO-R3-312, ADR 0035)
#
# Four readings a human operator could not get over REST before: what the responder
# says it is doing, and the three platform numbers that were MCP-only. Every one is
# `support|admin` and tenant-scoped exactly as the rest of this router is — the point
# of ADR 0035 is that this half of the pair is operator-only, so none of it is
# reachable by a machine principal's token.
# ---------------------------------------------------------------------------


@router.get("/agent-runs", response_model=PaginatedResponse[AgentRunSummaryResponse])
async def admin_list_agent_runs(
    params: AgentRunListParams = Depends(),
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[AgentRunSummaryResponse]:
    """Runs reported by an autonomous responder, newest first.

    `?active=true` narrows to runs nobody has closed — the console's own query while a
    demo is running. `?alert_id=` narrows to one alert's runs.

    The items carry everything but the step ledger (WO-R3-328): a page of 100 runs with
    200 steps each is megabytes nobody asked for, so the ledger is absent here — not
    emptied — and lives on the single-run read and `.../steps`.
    """
    effective_tenant = await resolve_admin_tenant(current_user, db, tenant_id)
    runs, total = await AgentRunRepository(db).list_for_tenant(
        effective_tenant,
        alert_id=params.alert_id,
        active=params.active,
        offset=params.offset,
        limit=params.page_size,
    )
    return PaginatedResponse.build(
        items=[AgentRunSummaryResponse.model_validate(r) for r in runs],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/agent-runs/{run_id}", response_model=AgentRunResponse)
async def admin_get_agent_run(
    run_id: uuid.UUID,
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
) -> AgentRunResponse:
    """One run with its whole phase history and its briefing, if it has one."""
    from app.core.exceptions import NotFoundError

    effective_tenant = await resolve_admin_tenant(current_user, db, tenant_id)
    run = await AgentRunRepository(db).get_for_tenant(run_id, effective_tenant)
    if run is None:
        # 404 for another tenant's run as well as a missing one: the id space stays
        # opaque, exactly as `JobRepository.get_for_tenant` keeps it.
        raise NotFoundError(f"Agent run {run_id} not found")
    return AgentRunResponse.model_validate(run)


@router.get("/agent-runs/{run_id}/steps", response_model=AgentRunStepsResponse)
async def admin_agent_run_steps(
    run_id: uuid.UUID,
    after_seq: int | None = Query(
        default=None,
        ge=0,
        description=(
            "Return only steps whose `seq` is greater than this. Omit for the whole "
            "ledger. Send back `next_after_seq` from the previous reply to poll."
        ),
    ),
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
) -> AgentRunStepsResponse:
    """One run's action ledger, oldest first, for a console polling it (WO-R3-328).

    A tail read rather than an offset page: `?after_seq=` is the last `seq` the caller
    already drew, so a panel updating twice a second asks for what is new instead of
    re-reading a 200-entry ledger — and a step it has already shown cannot arrive twice.

    Sorted here by `seq` rather than trusted in stored order: the responder appends in
    the order it reports, and a report that arrived out of order would otherwise put a
    step in the wrong place on a screen.
    """
    from app.core.exceptions import NotFoundError

    effective_tenant = await resolve_admin_tenant(current_user, db, tenant_id)
    run = await AgentRunRepository(db).get_for_tenant(run_id, effective_tenant)
    if run is None:
        # 404 for another tenant's run as well as a missing one, exactly as the
        # single-run read keeps the id space opaque.
        raise NotFoundError(f"Agent run {run_id} not found")

    stored = sorted(run.steps or [], key=lambda s: _step_seq(s))
    selected = [
        s for s in stored if after_seq is None or _step_seq(s) > after_seq
    ]
    highest = _step_seq(stored[-1]) if stored else None
    return AgentRunStepsResponse(
        run_id=run.id,
        state=run.state,
        finished_at=run.finished_at,
        steps=[AgentRunStepResponse.model_validate(s) for s in selected],
        returned=len(selected),
        total=len(stored),
        steps_dropped=run.steps_dropped or 0,
        after_seq=after_seq,
        # The highest `seq` STORED, not the highest returned: a poll that finds nothing
        # new must not hand back a cursor that re-reads the tail next time.
        next_after_seq=highest if highest is not None else after_seq,
    )


def _step_seq(step: dict[str, Any]) -> int:
    """A step's position, with an unusable one sorted to the front.

    The write surface requires an integer `seq`, so this is defence against a row
    written before that surface existed rather than an expected shape.
    """
    raw = step.get("seq")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else -1


@router.get("/consumer-lag", response_model=ConsumerLagResponse)
async def admin_consumer_lag(
    current_user: User = Depends(_require_support_or_admin),
    redis: Redis = Depends(get_redis),
) -> ConsumerLagResponse:
    """Every consumer group the platform tracks, in one reading.

    The same arithmetic the agent's `get_consumer_lag` uses (`app/core/consumer_lag.py`),
    so the console and the agent cannot disagree about whether a lag is known. Unlike
    that tool this takes no group argument: an operator watching a queue drain wants
    every group at once. Not tenant-scoped, and it says so — consumer groups are
    platform-wide.
    """
    groups: list[ConsumerLagGroupResponse] = []
    for name in SEEDED_CONSUMER_GROUPS:
        reading = await read_lag(redis, name)
        groups.append(
            ConsumerLagGroupResponse(
                consumer_group=reading.consumer_group,
                lag=reading.lag,
                lag_known=reading.lag_known,
                source=reading.source,
                lag_unknown_reason=_lag_unknown_reason(reading),
                measured_at=reading.measured_at,
                age_seconds=reading.age_seconds,
                recent_samples=[
                    ConsumerLagSampleResponse(lag=s.lag, measured_at=s.measured_at)
                    for s in reading.recent_samples
                ],
            )
        )
    return ConsumerLagResponse(
        measured_at=datetime.now(UTC),
        groups=groups,
        total=len(groups),
        live_group=LIVE_REFRESHED_GROUP,
        sample_window_seconds=LAG_SAMPLES_WINDOW_SECONDS,
        sample_interval_seconds=LAG_SAMPLES_WINDOW_SECONDS // LAG_SAMPLES_KEEP,
    )


def _lag_unknown_reason(reading: LagReading) -> str | None:
    """Why this group has no number, in words an operator can act on.

    Never a zero and never a blank: a missing lag on a group that should carry a
    recorded constant is an environment problem, and a missing one on the live group
    means the metrics loop has not reported inside the cache's TTL. Those are different
    jobs for the person reading the screen.
    """
    if reading.lag_known:
        return None
    if reading.source == "live":
        return (
            "no lag has been recorded for this group inside the cache window — the "
            "metrics loop has not reported recently, or the worker is not running"
        )
    if reading.source == "static":
        return (
            "this group's value is a recorded constant and the record is absent — an "
            "environment problem to fix, not a queue to investigate"
        )
    return "this platform does not track a consumer group by that name"


@router.get("/circuit-breakers", response_model=CircuitBreakersResponse)
async def admin_circuit_breakers(
    current_user: User = Depends(_require_support_or_admin),
    redis: Redis = Depends(get_redis),
) -> CircuitBreakersResponse:
    """Every breaker with a published state record (ADR 0030).

    A breaker with no record is absent from the list, never reported closed, and an
    empty list with `unknown_reason` set means the platform could say nothing. Breakers
    are platform-wide, so this reading is not tenant-scoped.
    """
    records, unknown_reason = await read_breaker_states(redis)
    measured_at = datetime.now(UTC)
    breakers = [
        CircuitBreakerResponse(
            name=r.name,
            state=r.state,
            failure_count=r.failure_count,
            failure_threshold=r.failure_threshold,
            recovery_timeout_s=r.recovery_timeout_s,
            last_state_change_at=r.last_state_change_at,
            seconds_since_state_change=_age_s(measured_at, r.last_state_change_at),
            last_failure_at=r.last_failure_at,
            last_failure_reason_class=r.last_failure_reason_class,
            recorded_at=r.recorded_at,
            reported_age_s=_age_s(measured_at, r.recorded_at) or 0.0,
        )
        for r in records
    ]
    return CircuitBreakersResponse(
        measured_at=measured_at,
        breakers=breakers,
        total=len(breakers),
        unknown_reason=unknown_reason,
    )


def _age_s(measured_at: datetime, at: datetime | None) -> float | None:
    """Seconds between a timestamp and the reading, clamped at 0 for clock skew.

    Same arithmetic as `get_circuit_breakers`: the timestamps come from the process
    that owns the breaker and the reading from this one, so an age compares two clocks.
    """
    if at is None:
        return None
    return round(max(0.0, (measured_at - at).total_seconds()), 3)


@router.get("/alerts", response_model=PaginatedResponse[AlertResponse])
async def admin_list_alerts(
    params: AlertListParams = Depends(),
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[AlertResponse]:
    """Alerts for the caller's tenant, newest first.

    `?active=true` is the agent's `list_active_alerts` view; omitting it shows resolved
    alerts too, which is the one thing that tool never returns and the thing a human
    reading a timeline after the fact needs.
    """
    effective_tenant = await resolve_admin_tenant(current_user, db, tenant_id)
    alerts, total = await AlertRepository(db).list_for_tenant(
        effective_tenant,
        active=params.active,
        severity=params.severity,
        offset=params.offset,
        limit=params.page_size,
    )
    return PaginatedResponse.build(
        items=[AlertResponse.model_validate(a) for a in alerts],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/users", response_model=PaginatedResponse[UserResponse])
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
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
