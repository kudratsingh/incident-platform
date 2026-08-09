"""SSE stream auth: the stream-token mint endpoint and the stream route.

The browser cannot send an Authorization header from a native EventSource, so
the stream authenticates with a short-lived, single-purpose stream token:

  POST /jobs/{id}/stream-token   — normal header auth; authorizes the job
                                   (tenant scope + ownership) and mints the token
  GET  /jobs/{id}/stream?token=… — validates the stream token from the query
                                   string; identity comes only from the token

Covered here:
  - minting requires authentication (401 with no header)
  - minting enforces tenant scope (404 cross-tenant — cannot confirm existence)
  - minting enforces ownership (403 for a non-owner plain user; admins allowed)
  - the minted token is bound to the job (sub claim == job id)
  - the stream route rejects: missing token, the primary access JWT, an
    expired stream token, and a stream token minted for a different job
  - the stream route accepts a freshly minted stream token (no more 401 loop)

The SSE generator itself is not drained — conftest's Redis is an AsyncMock, so
the accept test patches app.api.streaming.subscribe with an empty async
generator and asserts on the auth decision only.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest_asyncio
from app.config import get_settings
from app.core.security import create_access_token, decode_token, hash_password
from app.models.enums import JobStatus, JobType, UserRole
from app.models.job import Job
from app.models.tenant import Tenant
from app.models.user import User
from app.workers.progress import ProgressEvent
from httpx import AsyncClient
from jose import jwt

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def job(db_session, test_user: User) -> Job:  # type: ignore[no-untyped-def]
    """A running job owned by the conftest test_user (default tenant)."""
    job = Job(
        tenant_id=test_user.tenant_id,
        user_id=test_user.id,
        type=JobType.CSV_UPLOAD,
        status=JobStatus.RUNNING,
        retry_count=0,
        max_retries=3,
        priority=0,
    )
    db_session.add(job)
    await db_session.flush()
    return job


@pytest_asyncio.fixture
async def other_tenant_headers(db_session) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """Auth headers for a user in a completely separate tenant."""
    tenant = Tenant(
        id=uuid.uuid4(), slug="stream-other-tenant", name="Other Co.", is_active=True
    )
    db_session.add(tenant)
    await db_session.flush()
    user = User(
        tenant_id=tenant.id,
        email="stream-outsider@other.example.com",
        hashed_password=hash_password("password123"),
        role=UserRole.USER,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    token = create_access_token(
        {"sub": str(user.id), "tenant_id": str(user.tenant_id), "role": user.role}
    )
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def second_user_headers(db_session, default_tenant) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """Auth headers for a second plain (non-admin) user in the default tenant."""
    user = User(
        tenant_id=default_tenant.id,
        email="stream-second@example.com",
        hashed_password=hash_password("password123"),
        role=UserRole.USER,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    token = create_access_token(
        {"sub": str(user.id), "tenant_id": str(user.tenant_id), "role": user.role}
    )
    return {"Authorization": f"Bearer {token}"}


def _expired_stream_token(job_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    """A structurally valid stream token whose 60s lifetime has already passed.

    Built by back-dating the mint-time clock (the sanctioned way to test
    expiry — the real TTL is never extended for test convenience).
    """
    settings = get_settings()
    minted_at = datetime.now(UTC) - timedelta(seconds=120)
    payload = {
        "sub": str(job_id),
        "tenant_id": str(tenant_id),
        "exp": minted_at + timedelta(seconds=60),
        "iat": minted_at,
        "type": "stream",
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


async def _empty_subscribe(*args: object) -> AsyncGenerator[ProgressEvent, None]:
    """Stand-in for progress.subscribe — yields nothing and ends the stream."""
    return
    yield  # pragma: no cover — unreachable; makes this an async generator


# ---------------------------------------------------------------------------
# POST /jobs/{id}/stream-token — authn + authz (the missing F1-03 checks)
# ---------------------------------------------------------------------------


async def test_stream_token_requires_auth(client: AsyncClient, job: Job) -> None:
    """Minting a stream token with no Authorization header → 401."""
    resp = await client.post(f"/api/v1/jobs/{job.id}/stream-token")
    assert resp.status_code == 401


async def test_stream_token_cross_tenant_404(
    client: AsyncClient, job: Job, other_tenant_headers: dict[str, str]
) -> None:
    """Tenant-B user asks for a stream token on a tenant-A job → 404.

    404, not 403 — the caller cannot even confirm the job exists.
    """
    resp = await client.post(
        f"/api/v1/jobs/{job.id}/stream-token", headers=other_tenant_headers
    )
    assert resp.status_code == 404
    assert resp.json().get("error_code") == "not_found"


async def test_stream_token_non_owner_403(
    client: AsyncClient, job: Job, second_user_headers: dict[str, str]
) -> None:
    """A plain user in the same tenant, not the job owner → 403."""
    resp = await client.post(
        f"/api/v1/jobs/{job.id}/stream-token", headers=second_user_headers
    )
    assert resp.status_code == 403
    assert resp.json().get("error_code") == "forbidden"


async def test_stream_token_owner_gets_job_bound_token(
    client: AsyncClient, job: Job, auth_headers: dict[str, str]
) -> None:
    """The owner gets a token whose subject is the JOB id (not the user id)."""
    resp = await client.post(
        f"/api/v1/jobs/{job.id}/stream-token", headers=auth_headers
    )
    assert resp.status_code == 200
    token = resp.json()["token"]
    payload = decode_token(token, expected_type="stream")
    assert payload["sub"] == str(job.id)
    assert payload["tenant_id"] == str(job.tenant_id)


async def test_stream_token_admin_can_mint_for_another_users_job(
    client: AsyncClient, job: Job, admin_headers: dict[str, str]
) -> None:
    """Admins keep the documented privilege of streaming any same-tenant job."""
    resp = await client.post(
        f"/api/v1/jobs/{job.id}/stream-token", headers=admin_headers
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /jobs/{id}/stream — token validation (the F2-01/F2-03 transport)
# ---------------------------------------------------------------------------


async def test_stream_route_rejects_missing_token(
    client: AsyncClient, job: Job
) -> None:
    """No ?token → 401 from OUR token check, not from the header-only scheme.

    The error_code assertion is what proves the route stopped depending on
    the Authorization header: at HEAD the 401 is FastAPI's bare
    {"detail": "Not authenticated"} with no error_code envelope.
    """
    resp = await client.get(f"/api/v1/jobs/{job.id}/stream")
    assert resp.status_code == 401
    assert resp.json().get("error_code") == "authentication_failed"


async def test_stream_route_rejects_primary_access_token(
    client: AsyncClient, job: Job, user_token: str
) -> None:
    """The primary access JWT in the query string must NOT open a stream.

    Single-purpose enforcement (F2-03): only type=stream tokens are accepted,
    so nobody can 'fix' browser auth by pasting the real JWT into the URL.
    """
    resp = await client.get(
        f"/api/v1/jobs/{job.id}/stream", params={"token": user_token}
    )
    assert resp.status_code == 401
    assert resp.json().get("error_code") == "authentication_failed"


async def test_stream_route_rejects_expired_token(
    client: AsyncClient, job: Job
) -> None:
    """A stream token minted 2 minutes ago (60s TTL) → 401."""
    expired = _expired_stream_token(job.id, job.tenant_id)
    resp = await client.get(
        f"/api/v1/jobs/{job.id}/stream", params={"token": expired}
    )
    assert resp.status_code == 401
    assert resp.json().get("error_code") == "authentication_failed"


async def test_stream_route_rejects_token_for_other_job(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    job: Job,
    test_user: User,
    auth_headers: dict[str, str],
) -> None:
    """A token minted for job A replayed against job B → 403.

    Both jobs belong to the same owner, so the ONLY thing failing here is the
    job binding — without it, any token the caller could mint would open any
    job's stream and reopen F1-03 through the back door.
    """
    other_job = Job(
        tenant_id=test_user.tenant_id,
        user_id=test_user.id,
        type=JobType.REPORT_GEN,
        status=JobStatus.RUNNING,
        retry_count=0,
        max_retries=3,
        priority=0,
    )
    db_session.add(other_job)
    await db_session.flush()

    mint = await client.post(f"/api/v1/jobs/{job.id}/stream-token", headers=auth_headers)
    assert mint.status_code == 200
    token_for_job_a = mint.json()["token"]

    resp = await client.get(
        f"/api/v1/jobs/{other_job.id}/stream", params={"token": token_for_job_a}
    )
    assert resp.status_code == 403
    assert resp.json().get("error_code") == "forbidden"


async def test_stream_route_accepts_valid_stream_token(
    client: AsyncClient, job: Job, auth_headers: dict[str, str]
) -> None:
    """Mint → stream with ?token= → NOT 401 (the F2-01 browser regression).

    At HEAD the header-only dependency 401'd every EventSource request, which
    is why the browser reconnect-looped forever. subscribe() is patched to an
    empty async generator because conftest's Redis is an AsyncMock — the
    assertion is the auth decision, not event delivery.
    """
    mint = await client.post(f"/api/v1/jobs/{job.id}/stream-token", headers=auth_headers)
    assert mint.status_code == 200
    stream_token = mint.json()["token"]

    with mock.patch("app.api.streaming.subscribe", _empty_subscribe):
        resp = await client.get(
            f"/api/v1/jobs/{job.id}/stream", params={"token": stream_token}
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
