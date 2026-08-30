"""Per-tenant rate-limit + monthly quota enforcement.

These exercise the helper directly against the in-memory SQLite session +
a mock Redis. The integration-level "POST /jobs returns 429" path is
covered by the API layer.
"""

import uuid
from unittest.mock import AsyncMock

import pytest
from app.core.exceptions import RateLimitError
from app.models.enums import JobStatus
from app.models.job import Job
from app.models.tenant import DEFAULT_TENANT_ID
from app.utils.quota import QuotaExceededError, check_tenant_limits
from sqlalchemy.ext.asyncio import AsyncSession


def _redis() -> AsyncMock:
    r = AsyncMock()
    r.incr = AsyncMock(return_value=1)
    r.expire = AsyncMock(return_value=True)
    return r


async def test_zero_limits_pass(
    db_session: AsyncSession, default_tenant
) -> None:
    """rate_limit=0 and quota=0 disable enforcement entirely."""
    default_tenant.rate_limit_per_minute = 0
    default_tenant.quota_jobs_per_month = 0
    await db_session.flush()
    await check_tenant_limits(db_session, _redis(), DEFAULT_TENANT_ID)


async def test_rate_limit_exceeded(
    db_session: AsyncSession, default_tenant
) -> None:
    default_tenant.rate_limit_per_minute = 10
    await db_session.flush()
    r = _redis()
    r.incr = AsyncMock(return_value=11)  # one over the cap
    with pytest.raises(RateLimitError) as exc:
        await check_tenant_limits(db_session, r, DEFAULT_TENANT_ID)
    assert exc.value.details["scope"] == "tenant"
    assert exc.value.details["limit"] == 10


async def test_rate_limit_fails_open_when_redis_dies(
    db_session: AsyncSession, default_tenant
) -> None:
    """Redis outage must not block legitimate traffic — quota still applies."""
    default_tenant.rate_limit_per_minute = 10
    default_tenant.quota_jobs_per_month = 0
    await db_session.flush()
    r = _redis()
    r.incr = AsyncMock(side_effect=RuntimeError("redis down"))
    # Should not raise.
    await check_tenant_limits(db_session, r, DEFAULT_TENANT_ID)


async def test_monthly_quota_exceeded(
    db_session: AsyncSession, default_tenant, test_user
) -> None:
    default_tenant.rate_limit_per_minute = 0
    default_tenant.quota_jobs_per_month = 2
    await db_session.flush()

    # Seed two jobs this month — already at the cap.
    for _ in range(2):
        db_session.add(
            Job(
                id=uuid.uuid4(),
                tenant_id=default_tenant.id,
                user_id=test_user.id,
                type="csv_upload",
                status=JobStatus.PENDING,
                priority=5,
                payload={},
            )
        )
    await db_session.flush()

    with pytest.raises(QuotaExceededError) as exc:
        await check_tenant_limits(db_session, _redis(), DEFAULT_TENANT_ID)
    assert exc.value.error_code == "quota_exceeded"
    assert "Monthly job quota reached" in str(exc.value)


async def test_unknown_tenant_rejected(db_session: AsyncSession) -> None:
    with pytest.raises(QuotaExceededError):
        await check_tenant_limits(db_session, _redis(), uuid.uuid4())


# ---------------------------------------------------------------------------
# job_count — a request that creates N jobs is checked as N (WO-R2-12)
#
# `POST /sagas` creates one job row per step. Checking it as a single job let
# a saga cross the cap by N-1 rows; checking it as N refuses it before the
# first INSERT, so a saga never commits half a chain and then meets the cap.
# The default of 1 keeps `POST /jobs` byte-identical: `used + 1 > cap` is the
# same predicate as the `used >= cap` it replaced.
# ---------------------------------------------------------------------------


async def _seed_jobs(  # type: ignore[no-untyped-def]
    db_session: AsyncSession, tenant, user, count: int
) -> None:
    for _ in range(count):
        db_session.add(
            Job(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                user_id=user.id,
                type="csv_upload",
                status=JobStatus.PENDING,
                priority=5,
                payload={},
            )
        )
    await db_session.flush()


async def test_job_count_rejects_a_batch_that_would_cross_the_cap(
    db_session: AsyncSession, default_tenant, test_user
) -> None:
    """Under the cap, but not by enough for this batch."""
    default_tenant.rate_limit_per_minute = 0
    default_tenant.quota_jobs_per_month = 10
    await db_session.flush()
    await _seed_jobs(db_session, default_tenant, test_user, 8)

    with pytest.raises(QuotaExceededError) as exc:
        await check_tenant_limits(
            db_session, _redis(), DEFAULT_TENANT_ID, job_count=5
        )
    assert exc.value.details == {"limit": 10, "used": 8, "requested": 5}


async def test_job_count_allows_a_batch_that_exactly_fits(
    db_session: AsyncSession, default_tenant, test_user
) -> None:
    """8 used + 2 requested against a cap of 10 is the last batch that fits."""
    default_tenant.rate_limit_per_minute = 0
    default_tenant.quota_jobs_per_month = 10
    await db_session.flush()
    await _seed_jobs(db_session, default_tenant, test_user, 8)

    await check_tenant_limits(db_session, _redis(), DEFAULT_TENANT_ID, job_count=2)


async def test_single_job_semantics_are_unchanged(
    db_session: AsyncSession, default_tenant, test_user
) -> None:
    """The default job_count=1 must be exactly the old `used >= cap` rule.

    At the cap it refuses; one under it accepts. `POST /jobs` behaviour does
    not move because the batch parameter exists.
    """
    default_tenant.rate_limit_per_minute = 0
    default_tenant.quota_jobs_per_month = 3
    await db_session.flush()
    await _seed_jobs(db_session, default_tenant, test_user, 2)

    # One under the cap — accepted.
    await check_tenant_limits(db_session, _redis(), DEFAULT_TENANT_ID)

    await _seed_jobs(db_session, default_tenant, test_user, 1)  # now at 3/3
    with pytest.raises(QuotaExceededError):
        await check_tenant_limits(db_session, _redis(), DEFAULT_TENANT_ID)


async def test_job_count_is_ignored_when_the_quota_is_disabled(
    db_session: AsyncSession, default_tenant, test_user
) -> None:
    default_tenant.rate_limit_per_minute = 0
    default_tenant.quota_jobs_per_month = 0
    await db_session.flush()
    await _seed_jobs(db_session, default_tenant, test_user, 5)

    await check_tenant_limits(
        db_session, _redis(), DEFAULT_TENANT_ID, job_count=10_000
    )


async def test_a_batch_counts_as_one_request_against_the_tenant_rate_limit(
    db_session: AsyncSession, default_tenant
) -> None:
    """job_count bounds the monthly JOB quota, not the per-minute REQUEST rate.

    `tenants.rate_limit_per_minute` counts requests, and one saga is one
    request; multiplying it there would silently redefine a configured
    column. Volume is the quota's job to bound, and it does.
    """
    default_tenant.rate_limit_per_minute = 10
    default_tenant.quota_jobs_per_month = 0
    await db_session.flush()
    r = _redis()

    await check_tenant_limits(db_session, r, DEFAULT_TENANT_ID, job_count=50)

    assert r.incr.await_count == 1
