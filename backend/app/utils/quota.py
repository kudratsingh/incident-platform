"""
Per-tenant rate limit + monthly quota check.

Two enforcement mechanisms, both scoped to the authenticated tenant:

  * Rate limit: requests per minute, sliding window in Redis. The cap is
    `tenants.rate_limit_per_minute`; 0 disables.
  * Monthly quota: total jobs the tenant created in the current UTC
    calendar month. The cap is `tenants.quota_jobs_per_month`; 0 disables.

The quota is a per-request SQL count rather than a Redis counter:

  * The set of tenants creating jobs in a given minute is small, so the
    extra query is cheap (it hits the (tenant_id, created_at) index).
  * It survives Redis restarts and stays consistent with the source of
    truth, which matters more than micro-latency for the cap.
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


async def _check_monthly_quota(session: AsyncSession, tenant: Tenant) -> None:
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
    if used >= tenant.quota_jobs_per_month:
        raise QuotaExceededError(
            f"Monthly job quota reached for tenant {tenant.slug} "
            f"({used} / {tenant.quota_jobs_per_month}). "
            "Quota resets at the start of next month."
        )


async def check_tenant_limits(
    session: AsyncSession, redis: Redis, tenant_id: uuid.UUID
) -> None:
    """Single entry point used at the top of POST /jobs.

    Raises RateLimitError (429) when the per-minute cap is hit,
    QuotaExceededError (429, error_code=quota_exceeded) when the monthly cap is hit.
    """
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant is None:
        raise QuotaExceededError(f"Tenant {tenant_id} not found")
    await _check_tenant_rate(redis, tenant)
    await _check_monthly_quota(session, tenant)
