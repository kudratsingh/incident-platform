"""
Per-tenant rate limit + monthly quota check.

Two enforcement mechanisms, both scoped to the authenticated tenant:

  * Rate limit: requests per minute, fixed window in Redis (it resets on
    absolute boundaries rather than moving with the caller, so the real
    bound is 2x the cap across a boundary instant — see
    `app/utils/rate_limit.py`). The cap is `tenants.rate_limit_per_minute`;
    0 disables.
  * Monthly quota: total jobs the tenant created in the current UTC
    calendar month. The cap is `tenants.quota_jobs_per_month`; 0 disables.
    Checked against the number of jobs the request is about to create
    (`job_count`), so a 50-step saga is weighed as 50 jobs rather than one.

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


async def _check_monthly_quota(
    session: AsyncSession, tenant: Tenant, job_count: int = 1
) -> None:
    """Refuse the request if the `job_count` rows it creates cross the cap.

    Checked as a batch rather than per row, and *before* the first INSERT:
    `POST /sagas` creates one job per step, so a saga that ran this check as
    a single job could overshoot the cap by N-1 rows, and one that ran it per
    step would commit part of its chain before meeting the cap mid-loop.

    `job_count=1` is exactly the `used >= cap` rule this replaced —
    `used + 1 > cap` is the same predicate — so `POST /jobs` does not move.
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

    Reached through `utils/admission.check_job_admission`, which both
    `POST /jobs` and `POST /sagas` call.

    `job_count` is how many `jobs` rows this request will create — 1 for
    `POST /jobs`, `len(steps)` for `POST /sagas`. It applies to the monthly
    quota only, whose unit is jobs. The per-minute limit is deliberately left
    at one increment per request: its column is `tenants.rate_limit_per_minute`
    and its unit is requests, so multiplying it there would silently redefine
    a configured value. Bounding volume is the quota's job, and it does it.

    Raises RateLimitError (429) when the per-minute cap is hit,
    QuotaExceededError (429, error_code=quota_exceeded) when the monthly cap is hit.
    """
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant is None:
        raise QuotaExceededError(f"Tenant {tenant_id} not found")
    await _check_tenant_rate(redis, tenant)
    await _check_monthly_quota(session, tenant, job_count)
