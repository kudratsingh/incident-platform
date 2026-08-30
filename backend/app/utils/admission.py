"""
Admission control for job-creating endpoints — the preconditions every
surface that inserts `jobs` rows must run before it inserts them.

Why this exists as one helper rather than three calls per endpoint
------------------------------------------------------------------
`POST /jobs` ran three preconditions (per-IP rate limit, backpressure,
per-tenant rate limit + monthly quota) and `POST /sagas` ran none — so the
per-tenant monthly quota, the rate limit and backpressure were all bypassable
by sending the same work through the saga endpoint (WO-R2-12).  That was not a
missing line so much as a missing *place to put the line*: each endpoint was
expected to remember three separate imports in the right order.

`check_job_admission` is that place.  A new job-creating surface calls one
function and inherits every control, including the ones added after it was
written.  The per-IP rate limit stays a FastAPI dependency (it needs the
`Request` to identify the client) and is applied at the router with the same
`jobs:create` bucket both endpoints share.

Counting units
--------------
`job_count` is the number of `jobs` rows this request will create — 1 for
`POST /jobs`, `len(steps)` for `POST /sagas`.  It is applied to the *monthly
quota* only, because that cap counts jobs: `_check_monthly_quota` counts every
`Job` row, so saga-created steps consume the cap that blocks `POST /jobs`
while the saga endpoint itself was never blocked, which made the billing cap
unenforceable.  Checking `used + job_count > cap` up front also stops a saga
from committing half its steps and then hitting the cap mid-loop.

The per-tenant *rate* limit is deliberately NOT multiplied: its column is
`tenants.rate_limit_per_minute` and its unit is requests, not jobs.  One saga
request is one request.  Volume is the quota's job to bound, and it does.
"""

import uuid

from app.utils.backpressure import check_backpressure
from app.utils.quota import check_tenant_limits
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

# The per-IP rate-limit bucket shared by every endpoint that creates jobs.
# Shared, not per-endpoint, on purpose: separate buckets would leave the
# bypass half-open, letting a caller refused by POST /jobs carry on creating
# job rows through POST /sagas at the full rate.
JOB_CREATE_RATE_BUCKET = "jobs:create"


async def check_job_admission(
    session: AsyncSession,
    redis: Redis,
    tenant_id: uuid.UUID,
    *,
    job_count: int = 1,
) -> None:
    """Run every precondition for creating `job_count` jobs for this tenant.

    Raises:
        BackpressureError (503): the dispatcher is too far behind.
        RateLimitError (429): the tenant's per-minute request cap is hit.
        QuotaExceededError (429): these jobs would cross the monthly cap.

    Order matters and matches what `POST /jobs` has always done: backpressure
    first (cheapest, and a system-wide "not now" outranks a per-tenant one),
    then the tenant's own limits.  Both fail open on a Redis error — see
    `utils/backpressure.py` (PR #150) and `utils/quota.py`.
    """
    await check_backpressure(redis)
    await check_tenant_limits(session, redis, tenant_id, job_count=job_count)
