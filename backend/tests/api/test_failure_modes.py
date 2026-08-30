"""
Failure-mode tests — what happens when things go wrong.

Covers:
- Redis unavailable during job creation
- Redis unavailable during rate limit check (fail-open behaviour)
- Database error during job creation
- Malformed / missing request payloads
- Rate limit enforcement (429)
- Auth edge cases (expired token, tampered token, missing header)
- Job replay on non-failed job
- Accessing another user's job
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest_asyncio
from app.dependencies import get_db, get_redis
from app.main import create_app
from app.utils.backpressure import BACKPRESSURE_LAG_KEY
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Redis failure during job creation
# ---------------------------------------------------------------------------
#
# Assertion rule for everything in this section: a fail-open path must be
# pinned to the success status it is supposed to produce (201), never to
# `!= <the-rejection-code>`. A 500 also satisfies `!= 429`, so the negative
# form certifies fail-open while a fail-closed crash walks straight through
# it — which is exactly what happened to the backpressure check.


class _RedisDown:
    """A Redis stand-in whose every command raises, simulating a full outage.

    Deliberately not an ``AsyncMock`` with one method patched: the claim
    under test is "Redis is *down*", and a stub that answers some commands
    would let a fail-closed path pass by taking a branch that never touched
    the dead server.
    """

    def __getattr__(self, name: str) -> Any:
        async def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise ConnectionError(f"Redis unavailable (simulated): {name}")

        return _raise


class _RedisLagging(AsyncMock):
    """A reachable Redis that reports dispatcher lag far above the threshold."""

    async def get(self, key: str) -> bytes | None:
        if key == BACKPRESSURE_LAG_KEY:
            return b"999999"
        return None


@asynccontextmanager
async def _client_with_redis(
    db_session: AsyncSession, redis_obj: Any
) -> AsyncGenerator[AsyncClient, None]:
    """Build a test client whose `get_redis` dependency yields `redis_obj`.

    Mirrors the shared `client` fixture; only the Redis stand-in differs.
    """
    app = create_app()

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    async def _override_redis() -> AsyncGenerator[Any, None]:
        yield redis_obj

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        yield ac


@pytest_asyncio.fixture
async def redis_down_client(  # type: ignore[no-untyped-def]
    db_session: AsyncSession, default_tenant
) -> AsyncGenerator[AsyncClient, None]:
    """Like the shared `client` fixture, but Redis is completely unreachable."""
    async with _client_with_redis(db_session, _RedisDown()) as ac:
        yield ac


@pytest_asyncio.fixture
async def redis_lagging_client(  # type: ignore[no-untyped-def]
    db_session: AsyncSession, default_tenant
) -> AsyncGenerator[AsyncClient, None]:
    """Like the shared `client` fixture, but Redis reports a huge consumer lag."""
    async with _client_with_redis(db_session, _RedisLagging()) as ac:
        yield ac


async def test_job_create_still_works_when_redis_is_down(
    redis_down_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Redis fully down must not block job creation.

    The durable path is Postgres: `POST /jobs` writes the `jobs` row and the
    `outbox_events` row in one transaction, and the relay publishes to Kafka
    later. Every Redis touch on this path (per-client rate limit, backpressure
    lag cache, per-tenant rate limit) is advisory, so an outage degrades to
    "no signal, accept the job" — see docs/REDIS.md, "What breaks when Redis
    goes down".

    This test used to be an empty `pass` that merely described the invariant
    in a docstring. It reported green for the entire period during which
    `check_backpressure` did an unguarded GET and 500'd the whole endpoint.
    """
    resp = await redis_down_client.post(
        "/api/v1/jobs",
        json={"type": "csv_upload"},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "pending"


async def test_backpressure_fails_open_when_redis_get_raises(
    redis_down_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Narrow version of the test above, aimed at the backpressure GET only.

    The other two Redis touches on the path are stubbed out so a regression
    can only be attributed to `check_backpressure`. 201 — not "not 503", and
    not "not 429".
    """
    with (
        patch("app.utils.rate_limit._check"),
        patch("app.utils.quota._check_tenant_rate"),
    ):
        resp = await redis_down_client.post(
            "/api/v1/jobs",
            json={"type": "csv_upload"},
            headers=auth_headers,
        )
    assert resp.status_code == 201, resp.text


async def test_backpressure_still_rejects_when_redis_reports_high_lag(
    redis_lagging_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Failing open on *errors* must not turn into failing open on *signal*.

    A reachable Redis reporting lag above the threshold still produces the
    503. Without this, the fail-open tests above would be satisfied just as
    well by deleting the check altogether.
    """
    resp = await redis_lagging_client.post(
        "/api/v1/jobs",
        json={"type": "csv_upload"},
        headers=auth_headers,
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["error_code"] == "backpressure"


# ---------------------------------------------------------------------------
# Redis failure during rate limit check — fail-open
# ---------------------------------------------------------------------------


async def test_redis_down_on_rate_limit_fails_open(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """If Redis is down for the rate limiter the request should still go through
    (fail-open) rather than blocking all traffic with a 500.

    Asserts 201, not `!= 429`: the fail-closed outcome this test exists to
    rule out is a 500, which satisfies `!= 429` just as happily as the
    success it is meant to prove.
    """
    with patch(
        "app.utils.rate_limit._check",
        side_effect=ConnectionError("Redis unavailable"),
    ):
        resp = await client.post(
            "/api/v1/jobs",
            json={"type": "csv_upload"},
            headers=auth_headers,
        )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# Database error during job creation
# ---------------------------------------------------------------------------


async def test_db_error_on_job_create_returns_500(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    with patch(
        "app.repositories.job.JobRepository.create",
        side_effect=Exception("DB connection lost"),
    ):
        resp = await client.post(
            "/api/v1/jobs",
            json={"type": "csv_upload"},
            headers=auth_headers,
        )
    assert resp.status_code == 500


async def test_unhandled_error_returns_the_standard_error_envelope(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """An escaped non-AppError still answers in the documented shape.

    CLAUDE.md promises `error_code` / `message` / `details` / `request_id` on
    every error response. Without a catch-all handler, anything that isn't an
    `AppError` fell through to Starlette's bare `Internal Server Error`
    text/plain body — the one error shape a client is most likely to hit
    during an incident was the one shape it could not parse.
    """
    with patch(
        "app.repositories.job.JobRepository.create",
        side_effect=Exception("DB connection lost"),
    ):
        resp = await client.post(
            "/api/v1/jobs",
            json={"type": "csv_upload"},
            headers={**auth_headers, "X-Request-ID": "req-envelope-probe"},
        )
    assert resp.status_code == 500
    body = resp.json()
    assert body["error_code"] == "internal_error"
    assert body["request_id"] == "req-envelope-probe"
    # The raw exception text must not reach the client.
    assert "DB connection lost" not in resp.text


# ---------------------------------------------------------------------------
# Malformed payloads
# ---------------------------------------------------------------------------


async def test_missing_job_type_returns_422(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/api/v1/jobs",
        json={"payload": {"row_count": 100}},  # no "type"
        headers=auth_headers,
    )
    assert resp.status_code == 422


async def test_invalid_job_type_returns_422(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/api/v1/jobs",
        json={"type": "not_a_real_type"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


async def test_invalid_priority_type_returns_422(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/api/v1/jobs",
        json={"type": "csv_upload", "priority": "high"},  # should be int
        headers=auth_headers,
    )
    assert resp.status_code == 422


async def test_invalid_login_payload_returns_422(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": "notanemail"},  # missing password, wrong field name
    )
    assert resp.status_code == 422


async def test_empty_body_on_login_returns_422(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/login", json={})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Rate limit enforcement
# ---------------------------------------------------------------------------


async def test_rate_limit_returns_429(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Simulate hitting the rate limit by making the counter exceed the limit."""
    with patch(
        "app.utils.rate_limit._check",
        side_effect=__import__(
            "app.core.exceptions", fromlist=["RateLimitError"]
        ).RateLimitError("Rate limit exceeded: 10 requests per 60s."),
    ):
        resp = await client.post(
            "/api/v1/jobs",
            json={"type": "csv_upload"},
            headers=auth_headers,
        )
    assert resp.status_code == 429
    assert resp.json()["error_code"] == "rate_limit_exceeded"


# ---------------------------------------------------------------------------
# Auth edge cases
# ---------------------------------------------------------------------------


async def test_missing_auth_header_returns_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/jobs")
    assert resp.status_code == 401


async def test_malformed_token_returns_401(client: AsyncClient) -> None:
    resp = await client.get(
        "/api/v1/jobs",
        headers={"Authorization": "Bearer not.a.real.token"},
    )
    assert resp.status_code == 401


async def test_wrong_scheme_returns_401(client: AsyncClient) -> None:
    resp = await client.get(
        "/api/v1/jobs",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert resp.status_code == 401


async def test_login_wrong_password_returns_401(client: AsyncClient) -> None:
    # Register first
    await client.post(
        "/api/v1/auth/register",
        json={"email": "failure@example.com", "password": "correct-password"},
    )
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "failure@example.com", "password": "wrong-password"},
    )
    assert resp.status_code == 401


async def test_login_unknown_email_returns_401(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "doesntmatter"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Job access control
# ---------------------------------------------------------------------------


async def test_replay_non_failed_job_returns_400(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """Replaying a pending/running job should be rejected."""
    create_resp = await client.post(
        "/api/v1/jobs", json={"type": "csv_upload"}, headers=admin_headers
    )
    job_id = create_resp.json()["id"]

    resp = await client.post(
        f"/api/v1/admin/jobs/{job_id}/replay", headers=admin_headers
    )
    assert resp.status_code == 400
    assert resp.json()["error_code"] == "job_error"


async def test_non_admin_cannot_replay_job(
    client: AsyncClient,
    auth_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    create_resp = await client.post(
        "/api/v1/jobs", json={"type": "csv_upload"}, headers=admin_headers
    )
    job_id = create_resp.json()["id"]

    resp = await client.post(
        f"/api/v1/admin/jobs/{job_id}/replay", headers=auth_headers
    )
    assert resp.status_code == 403


async def test_get_nonexistent_job_returns_404(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.get(
        "/api/v1/jobs/00000000-0000-0000-0000-000000000000",
        headers=auth_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "not_found"
