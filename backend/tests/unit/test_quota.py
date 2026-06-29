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
