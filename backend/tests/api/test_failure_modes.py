"""Failure-mode tests: Redis down during job creation and during the rate-limit check, a database
error on create, malformed payloads, 429 enforcement, auth edge cases, replaying a non-failed job,
and reaching another user's job.
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

# Redis failure during job creation.
#
# Assertion rule for this section: a fail-open path is pinned to the success status it must produce
# (201), never to `!= <the rejection code>`. A 500 satisfies `!= 429` too, which is exactly what
# happened to the backpressure check.


class _RedisDown:
    """A Redis stand-in whose every command raises. Deliberately not an AsyncMock with one method
    patched: a stub that answers some commands lets a fail-closed path pass on a branch that never
    touched the dead server."""

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
    """Mirrors the shared `client` fixture; only the Redis stand-in differs."""
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
    """Redis fully down must not block job creation: the durable path is Postgres, and every Redis
    touch here is advisory (docs/REDIS.md). This test used to be an empty `pass` that described the
    invariant in a docstring, and reported green for the whole period `check_backpressure` 500'd the
    endpoint on an unguarded GET."""
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
    """Narrow version aimed at the backpressure GET alone: 201, not "not 503" and not "not 429"."""
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
    """Failing open on errors must not become failing open on signal: a reachable Redis reporting
    lag above the threshold still produces the 503."""
    resp = await redis_lagging_client.post(
        "/api/v1/jobs",
        json={"type": "csv_upload"},
        headers=auth_headers,
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["error_code"] == "backpressure"


# Redis failure during rate limit check — fail-open


async def test_redis_down_on_rate_limit_fails_open(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Fail-open for the rate limiter, asserted as 201 rather than `!= 429` — a 500 satisfies `!=
    429` just as happily as the success this is meant to prove."""
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


# Database error during job creation


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
    """An escaped non-AppError still answers in the documented shape. Without a catch-all handler
    anything that is not an `AppError` fell through to Starlette's text/plain body — the one error
    shape a client is most likely to hit during an incident was the one it could not parse."""
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


# Malformed payloads


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


# Rate limit enforcement


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


# Auth edge cases


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


# Job access control


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


# The same Redis posture on POST /sagas (WO-R2-12).
#
# It creates N job rows and used to run none of the three preconditions `POST /jobs` runs. Adding
# them has to add the whole posture: a check that rejects on signal but 500s on a Redis outage would
# be strictly worse than the bypass it replaced. Assertion rule as above.


async def test_saga_create_still_works_when_redis_is_down(
    redis_down_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Saga creation has the identical durable path: Postgres rows plus an outbox row in one
    transaction, and every Redis touch the admission guard adds is advisory."""
    resp = await redis_down_client.post(
        "/api/v1/sagas",
        json={"name": "redis-is-down", "steps": [{"type": "csv_upload"}]},
        headers=auth_headers,
    )

    assert resp.status_code == 201, resp.text
    assert len(resp.json()["steps"]) == 1


async def test_saga_backpressure_fails_open_when_redis_get_raises(
    redis_down_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Narrow version for the backpressure GET the saga path now makes, through the shared helper
    from PR #150 rather than a reimplementation."""
    with (
        patch("app.utils.rate_limit._check"),
        patch("app.utils.quota._check_tenant_rate"),
    ):
        resp = await redis_down_client.post(
            "/api/v1/sagas",
            json={"name": "backpressure-open", "steps": [{"type": "csv_upload"}]},
            headers=auth_headers,
        )

    assert resp.status_code == 201, resp.text


async def test_saga_create_is_refused_when_redis_reports_high_lag(
    redis_lagging_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """The backpressure half of WO-R2-12: pre-fix this returned 201 while `POST /jobs` returned 503
    against the very same Redis."""
    resp = await redis_lagging_client.post(
        "/api/v1/sagas",
        json={"name": "too-much-lag", "steps": [{"type": "csv_upload"}]},
        headers=auth_headers,
    )

    assert resp.status_code == 503, resp.text
    assert resp.json()["error_code"] == "backpressure"


class _RedisCountingKeys(AsyncMock):
    """A reachable Redis that records which keys were INCR'd."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.incr_keys: list[str] = []

    async def incr(self, key: str) -> int:
        self.incr_keys.append(key)
        return 1

    async def get(self, key: str) -> bytes | None:
        return None


async def test_jobs_and_sagas_share_one_job_creation_rate_bucket(
    db_session: AsyncSession, default_tenant: Any, auth_headers: dict[str, str]
) -> None:
    """Both job-creating endpoints draw on the SAME per-IP budget; a separate bucket would let a
    caller refused by `POST /jobs` carry on through `POST /sagas`. Asserted on the limiter's key
    rather than by sending 31 requests against a wall-clock window."""
    redis = _RedisCountingKeys()
    async with _client_with_redis(db_session, redis) as ac:
        await ac.post("/api/v1/jobs", json={"type": "csv_upload"}, headers=auth_headers)
        job_keys = [k for k in redis.incr_keys if k.startswith("rate:jobs:create:")]

        redis.incr_keys.clear()
        await ac.post(
            "/api/v1/sagas",
            json={"name": "same-bucket", "steps": [{"type": "csv_upload"}]},
            headers=auth_headers,
        )
        saga_keys = [k for k in redis.incr_keys if k.startswith("rate:jobs:create:")]

    assert job_keys, "POST /jobs did not touch a jobs:create rate key"
    assert saga_keys == job_keys, (
        f"POST /sagas rate-limits on {saga_keys}, POST /jobs on {job_keys} — "
        "separate buckets leave the job-creation rate limit bypassable"
    )
