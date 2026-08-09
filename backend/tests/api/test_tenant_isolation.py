"""Cross-tenant isolation tests.

Verify that with the enforcement in place, a user in tenant A cannot
read, replay, or get a 4xx leak about a job belonging to tenant B.
"""

import json
import uuid
from unittest.mock import AsyncMock

import pytest_asyncio
from app.core.security import create_access_token, hash_password
from app.dependencies import get_redis
from app.models.enums import JobStatus, JobType, UserRole
from app.models.job import Job
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.job import JobResponse
from httpx import AsyncClient


@pytest_asyncio.fixture
async def other_tenant(db_session):  # type: ignore[no-untyped-def]
    """A second tenant — completely separate from the conftest default."""
    tenant = Tenant(
        id=uuid.uuid4(), slug="other-tenant", name="Other Co.", is_active=True
    )
    db_session.add(tenant)
    await db_session.flush()
    return tenant


@pytest_asyncio.fixture
async def other_tenant_user(db_session, other_tenant) -> User:  # type: ignore[no-untyped-def]
    user = User(
        tenant_id=other_tenant.id,
        email="alice@other.example.com",
        hashed_password=hash_password("password123"),
        role=UserRole.USER,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def other_tenant_headers(other_tenant_user: User) -> dict[str, str]:
    token = create_access_token(
        {
            "sub": str(other_tenant_user.id),
            "tenant_id": str(other_tenant_user.tenant_id),
            "role": other_tenant_user.role,
        }
    )
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def other_tenant_admin_headers(db_session, other_tenant) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """An admin scoped to tenant B — privileged within their own tenant, not ours."""
    admin = User(
        tenant_id=other_tenant.id,
        email="admin@other.example.com",
        hashed_password=hash_password("password123"),
        role=UserRole.ADMIN,
        is_active=True,
    )
    db_session.add(admin)
    await db_session.flush()
    token = create_access_token(
        {
            "sub": str(admin.id),
            "tenant_id": str(admin.tenant_id),
            "role": admin.role,
        }
    )
    return {"Authorization": f"Bearer {token}"}


async def test_user_cannot_get_job_from_other_tenant(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    test_user: User,
    other_tenant_user: User,
    other_tenant_headers: dict[str, str],
) -> None:
    """User in tenant A creates a job; user in tenant B asks for it → 404."""
    job = Job(
        tenant_id=test_user.tenant_id,
        user_id=test_user.id,
        type=JobType.CSV_UPLOAD,
        status=JobStatus.COMPLETED,
        retry_count=0,
        max_retries=3,
        priority=0,
    )
    db_session.add(job)
    await db_session.flush()

    resp = await client.get(
        f"/api/v1/jobs/{job.id}", headers=other_tenant_headers
    )
    # 404, not 403 — caller cannot even confirm the row exists.
    assert resp.status_code == 404


async def test_admin_cannot_get_job_from_other_tenant(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    test_user: User,
    other_tenant_admin_headers: dict[str, str],
) -> None:
    """Admin in tenant B is privileged inside B, but blind to tenant A's data."""
    job = Job(
        tenant_id=test_user.tenant_id,
        user_id=test_user.id,
        type=JobType.CSV_UPLOAD,
        status=JobStatus.DEAD_LETTER,
        retry_count=3,
        max_retries=3,
        priority=0,
        error_message="boom",
    )
    db_session.add(job)
    await db_session.flush()

    resp = await client.get(
        f"/api/v1/admin/jobs/{job.id}", headers=other_tenant_admin_headers
    )
    assert resp.status_code == 404


async def test_list_jobs_only_returns_same_tenant_rows(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    test_user: User,
    other_tenant_user: User,
    auth_headers: dict[str, str],
    other_tenant_headers: dict[str, str],
) -> None:
    """Insert one job per tenant; each user sees only their own in /jobs."""
    job_a = Job(
        tenant_id=test_user.tenant_id,
        user_id=test_user.id,
        type=JobType.CSV_UPLOAD,
        status=JobStatus.COMPLETED,
        retry_count=0,
        max_retries=3,
        priority=0,
    )
    job_b = Job(
        tenant_id=other_tenant_user.tenant_id,
        user_id=other_tenant_user.id,
        type=JobType.REPORT_GEN,
        status=JobStatus.COMPLETED,
        retry_count=0,
        max_retries=3,
        priority=0,
    )
    db_session.add(job_a)
    db_session.add(job_b)
    await db_session.flush()

    a_resp = await client.get("/api/v1/jobs", headers=auth_headers)
    b_resp = await client.get("/api/v1/jobs", headers=other_tenant_headers)
    assert a_resp.status_code == 200
    assert b_resp.status_code == 200

    a_ids = {item["id"] for item in a_resp.json()["items"]}
    b_ids = {item["id"] for item in b_resp.json()["items"]}
    assert str(job_a.id) in a_ids
    assert str(job_a.id) not in b_ids
    assert str(job_b.id) in b_ids
    assert str(job_b.id) not in a_ids


async def test_replay_rejects_cross_tenant_job(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    test_user: User,
    other_tenant_admin_headers: dict[str, str],
) -> None:
    """Admin from tenant B trying to replay a tenant-A DLQ job → 404."""
    job = Job(
        tenant_id=test_user.tenant_id,
        user_id=test_user.id,
        type=JobType.CSV_UPLOAD,
        status=JobStatus.DEAD_LETTER,
        retry_count=3,
        max_retries=3,
        priority=0,
        error_message="boom",
    )
    db_session.add(job)
    await db_session.flush()

    resp = await client.post(
        f"/api/v1/admin/jobs/{job.id}/replay",
        headers=other_tenant_admin_headers,
    )
    assert resp.status_code == 404


async def test_dlq_stats_scoped_to_tenant(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    test_user: User,
    other_tenant_user: User,
    admin_headers: dict[str, str],
    other_tenant_admin_headers: dict[str, str],
) -> None:
    """Each tenant's DLQ stats reflect only that tenant's dead-lettered jobs."""
    # 2 DLQ rows in tenant A, 5 in tenant B
    for _ in range(2):
        db_session.add(
            Job(
                tenant_id=test_user.tenant_id,
                user_id=test_user.id,
                type=JobType.CSV_UPLOAD,
                status=JobStatus.DEAD_LETTER,
                retry_count=3,
                max_retries=3,
                priority=0,
                error_message="a",
            )
        )
    for _ in range(5):
        db_session.add(
            Job(
                tenant_id=other_tenant_user.tenant_id,
                user_id=other_tenant_user.id,
                type=JobType.REPORT_GEN,
                status=JobStatus.DEAD_LETTER,
                retry_count=3,
                max_retries=3,
                priority=0,
                error_message="b",
            )
        )
    await db_session.flush()

    a_resp = await client.get("/api/v1/admin/dlq/stats", headers=admin_headers)
    b_resp = await client.get(
        "/api/v1/admin/dlq/stats", headers=other_tenant_admin_headers
    )
    assert a_resp.json()["total"] == 2
    assert b_resp.json()["total"] == 5


async def test_create_job_tags_with_callers_tenant(
    client: AsyncClient,
    other_tenant_headers: dict[str, str],
    other_tenant_user: User,
) -> None:
    """The new job lands in the caller's tenant — never silently in the default."""
    resp = await client.post(
        "/api/v1/jobs",
        json={"type": "csv_upload"},
        headers=other_tenant_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    # The returned shape doesn't expose tenant_id, but the SQL row must carry it.
    # Verify via the list endpoint — the job appears only under the same tenant.
    list_resp = await client.get(
        "/api/v1/jobs", headers=other_tenant_headers
    )
    ids = {item["id"] for item in list_resp.json()["items"]}
    assert body["id"] in ids


async def test_cache_hit_does_not_leak_cross_tenant_job(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    other_tenant,  # type: ignore[no-untyped-def]
    other_tenant_user: User,
    admin_headers: dict[str, str],
) -> None:
    """E2-01 regression: a cached tenant-B job must never be served to a
    privileged tenant-A caller. The cache key is tenant-scoped, so tenant A's
    lookup misses (`job:{A}:{id}`) and falls through to the tenant-scoped DB
    path, which 404s."""
    job = Job(
        tenant_id=other_tenant.id,
        user_id=other_tenant_user.id,
        type=JobType.CSV_UPLOAD,
        status=JobStatus.FAILED,
        retry_count=1,
        max_retries=3,
        priority=0,
        error_message="tenant B secret failure detail",
    )
    db_session.add(job)
    await db_session.flush()
    await db_session.refresh(job)

    cached_payload = json.dumps(
        JobResponse.model_validate(job).model_dump(mode="json")
    )

    # Simulate "tenant B's job sits in the Redis cache": serve the payload
    # for the tenant-B-scoped key (what fixed code writes) and for the
    # legacy global key (what unfixed code wrote — this is what makes the
    # test red at HEAD), but NEVER for a key scoped to any other tenant.
    # A naive mock answering every key could not tell a tenant-scoped
    # lookup from a global one and would mask the fix.
    tenant_b_keys = {f"job:{job.id}", f"job:{other_tenant.id}:{job.id}"}

    async def _tenant_aware_get(key: str) -> str | None:
        return cached_payload if key in tenant_b_keys else None

    async def _override_redis():  # type: ignore[no-untyped-def]
        mock = AsyncMock()
        mock.get = AsyncMock(side_effect=_tenant_aware_get)
        yield mock

    app = client._transport.app  # type: ignore[attr-defined]
    app.dependency_overrides[get_redis] = _override_redis

    # Privileged (ADMIN) caller in tenant A asks for tenant B's job.
    resp = await client.get(f"/api/v1/jobs/{job.id}", headers=admin_headers)

    # Cache miss on job:{A}:{id} -> DB path -> get_for_tenant(A) -> 404.
    assert resp.status_code == 404
    assert "tenant B secret failure detail" not in json.dumps(resp.json())


async def test_idempotency_key_reusable_across_tenants(
    client: AsyncClient,
    auth_headers: dict[str, str],
    other_tenant_headers: dict[str, str],
) -> None:
    """The same idempotency key in two tenants produces two distinct jobs —
    the unique constraint is scoped per-tenant, not global."""
    a_resp = await client.post(
        "/api/v1/jobs",
        json={"type": "csv_upload", "idempotency_key": "shared-key"},
        headers=auth_headers,
    )
    b_resp = await client.post(
        "/api/v1/jobs",
        json={"type": "csv_upload", "idempotency_key": "shared-key"},
        headers=other_tenant_headers,
    )
    assert a_resp.status_code == 201
    assert b_resp.status_code == 201
    assert a_resp.json()["id"] != b_resp.json()["id"]
