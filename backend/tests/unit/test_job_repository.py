"""Unit tests for `JobRepository`.

Focused on the update_status timestamp behavior. Uses monkey-patching of
`datetime.now` so we can assert on the exact value the repository writes,
independent of SQLite's tz-stripping behavior (SQLite silently drops
tzinfo when reading back from a DateTime(timezone=True) column, even
though it stores it).

The behavior we actually want to lock in is the write: the repository
must call `datetime.now(UTC)` (aware) and NOT `datetime.utcnow()`
(naive, deprecated). On real Postgres (`TIMESTAMPTZ`), aware writes
round-trip as aware; on SQLite we can only assert the intent.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from app.models.enums import JobStatus
from app.models.job import Job
from app.repositories.job import JobRepository
from sqlalchemy.ext.asyncio import AsyncSession


async def _make_job(db_session: AsyncSession, tenant_id: uuid.UUID) -> Job:
    user_id = uuid.uuid4()
    from app.core.security import hash_password
    from app.models.enums import UserRole
    from app.models.user import User

    user = User(
        id=user_id,
        tenant_id=tenant_id,
        email=f"{user_id}@test.example",
        hashed_password=hash_password("password123"),
        role=UserRole.USER,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()

    job = Job(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        user_id=user_id,
        type="csv_upload",
        status=JobStatus.PENDING,
        priority=0,
        payload={},
        retry_count=0,
        max_retries=3,
    )
    db_session.add(job)
    await db_session.flush()
    return job


@pytest.mark.asyncio
async def test_update_status_running_calls_datetime_now_utc(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """Regression: prior to the fix, `started_at` was written with naive
    `datetime.utcnow()`. Mixing naive here with aware `datetime.now(UTC)`
    in the SLO service raises TypeError on `(now - started_at)`.

    Locks in that the repository calls `datetime.now(UTC)` (aware),
    verifying the write-side intent regardless of the test DB's
    round-trip behavior.
    """
    job = await _make_job(db_session, default_tenant.id)
    repo = JobRepository(db_session)

    # Patch the module's `datetime.now` and assert it was called with UTC.
    aware_now = datetime.now(UTC)
    with patch("app.repositories.job.datetime") as mock_dt:
        mock_dt.now.return_value = aware_now
        # Preserve real behaviour for anything else the code touches.
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        await repo.update_status(job.id, JobStatus.RUNNING)

    # The one behaviour we lock in: `datetime.now` was called with UTC.
    mock_dt.now.assert_called_with(UTC)


@pytest.mark.asyncio
async def test_update_status_completed_calls_datetime_now_utc(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    job = await _make_job(db_session, default_tenant.id)
    repo = JobRepository(db_session)

    aware_now = datetime.now(UTC)
    with patch("app.repositories.job.datetime") as mock_dt:
        mock_dt.now.return_value = aware_now
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        await repo.update_status(job.id, JobStatus.COMPLETED)

    mock_dt.now.assert_called_with(UTC)
