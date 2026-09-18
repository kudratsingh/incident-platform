"""
Per-tenant rate limit + monthly quota check, both scoped to the authenticated
tenant; 0 disables either cap.

Rate limit: `tenants.rate_limit_per_minute`, fixed window in Redis, so the real
bound is 2x the cap across a boundary instant (`app/utils/rate_limit.py`). Quota:
`tenants.quota_jobs_per_month` against jobs created this UTC month, weighed by
`job_count` so a 50-step saga counts 50. The quota is a per-request SQL count on
the (tenant_id, created_at) index, not a Redis counter, so it survives restarts.
"""

import time
import uuid
from datetime import UTC, datetime

from app.core.exceptions import AppError, RateLimitError
from app.core.logging import get_logger
from app.models.job import Job
from app.models.tenant import Tenant
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)


class QuotaExceededError(AppError):
    status_code = 429
    error_code = "quota_exceeded"


def _month_start(now: datetime | None = None) -> datetime:
    """First instant of the current UTC calendar month."""
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _check_tenant_rate(redis: Redis, tenant: Tenant) -> None:
    if tenant.rate_limit_per_minute <= 0:
        return
    window_start = int(time.time()) // 60
    key = f"rate:tenant:{tenant.id}:{window_start}"
    try:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, 120)
    except Exception:
        # Redis unavailable — fail open. The monthly quota check still applies.
        logger.warning("tenant rate_limit_check_failed", extra={"tenant_id": str(tenant.id)})
        return
    if count > tenant.rate_limit_per_minute:
        raise RateLimitError(
            f"Tenant rate limit exceeded: {tenant.rate_limit_per_minute} requests/min.",
            details={
                "limit": tenant.rate_limit_per_minute,
                "window_seconds": 60,
                "scope": "tenant",
            },
        )


async def _check_monthly_quota(
    session: AsyncSession, tenant: Tenant, job_count: int = 1
) -> None:
    """Refuse the request if the `job_count` rows it creates cross the cap.

    As a batch and *before* the first INSERT, or a saga overshoots by N-1 rows or
    commits half its chain. `job_count=1` is the old `used >= cap` predicate.
    """
    if tenant.quota_jobs_per_month <= 0:
        return
    since = _month_start()
    used = (
        await session.execute(
            select(func.count())
            .select_from(Job)
            .where(Job.tenant_id == tenant.id, Job.created_at >= since)
        )
    ).scalar_one()
    if used + job_count > tenant.quota_jobs_per_month:
        requested = (
            f" This request would create {job_count} jobs." if job_count != 1 else ""
        )
        raise QuotaExceededError(
            f"Monthly job quota reached for tenant {tenant.slug} "
            f"({used} / {tenant.quota_jobs_per_month})."
            f"{requested} "
            "Quota resets at the start of next month.",
            details={
                "limit": tenant.quota_jobs_per_month,
                "used": used,
                "requested": job_count,
            },
        )


async def check_tenant_limits(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    *,
    job_count: int = 1,
) -> None:
    """The per-tenant half of admission control, for every job-creating surface.

    Reached through `utils/admission.check_job_admission`. `job_count` applies to
    the monthly quota only; the per-minute limit stays one increment per request
    because `tenants.rate_limit_per_minute` counts requests. Raises RateLimitError
    (429) or QuotaExceededError (429, error_code=quota_exceeded).
    """
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant is None:
        raise QuotaExceededError(f"Tenant {tenant_id} not found")
    await _check_tenant_rate(redis, tenant)
    await _check_monthly_quota(session, tenant, job_count)
