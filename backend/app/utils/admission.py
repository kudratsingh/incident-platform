"""
Admission control for job-creating endpoints — the preconditions every
surface that inserts `jobs` rows must run before it inserts them.

One helper because `POST /sagas` ran none of the three `POST /jobs` ran, making
quota, rate limit and backpressure bypassable (WO-R2-12). The per-IP limit stays a
FastAPI dependency (it needs the `Request`) on the shared `jobs:create` bucket.
`job_count` — 1 for `POST /jobs`, `len(steps)` for `POST /sagas` — applies to the
*monthly quota* only, checked up front so a saga cannot commit half its steps. The
per-tenant rate limit is NOT multiplied: `tenants.rate_limit_per_minute` counts
requests, not jobs.
"""

import uuid
from collections.abc import Callable, Coroutine
from typing import Any

from app.config import get_settings
from app.core.redis import get_redis
from app.utils.backpressure import check_backpressure
from app.utils.quota import check_tenant_limits
from app.utils.rate_limit import check_client_rate_limit
from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

# The per-IP rate-limit bucket shared by every endpoint that creates jobs.
# Per-endpoint buckets would leave the bypass half-open.
JOB_CREATE_RATE_BUCKET = "jobs:create"


def job_create_rate_limiter() -> Callable[..., Coroutine[Any, Any, None]]:
    """The per-address rate limit every job-creating endpoint declares.

    `JOB_CREATE_RATE_LIMIT` / `JOB_CREATE_RATE_WINDOW_SECONDS` (30, 60s) are read on
    every request, because the dependency is built once at import and a ceiling read
    there could never be changed by a deployment's environment (WO-R3-343).
    """

    async def dependency(
        request: Request,
        redis: Redis = Depends(get_redis),
    ) -> None:
        settings = get_settings()
        await check_client_rate_limit(
            request,
            redis,
            limit=settings.job_create_rate_limit,
            window=settings.job_create_rate_window_seconds,
            key_prefix=JOB_CREATE_RATE_BUCKET,
        )

    return dependency


async def check_job_admission(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    *,
    job_count: int = 1,
) -> None:
    """Run every precondition for creating `job_count` jobs for this tenant.

    Raises BackpressureError (503), RateLimitError (429) or QuotaExceededError
    (429). Backpressure first — a system-wide "not now" outranks a per-tenant one.
    Both fail open on a Redis error.
    """
    await check_backpressure(redis)
    await check_tenant_limits(session, redis, tenant_id, job_count=job_count)
