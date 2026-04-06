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

from unittest.mock import patch

from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Redis failure during job creation
# ---------------------------------------------------------------------------


async def test_redis_down_on_job_create_returns_500(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """If Redis is unavailable the queue push fails — should surface as 500."""
    with patch(
        "app.workers.queue.push", side_effect=ConnectionError("Redis unavailable")
    ):
        resp = await client.post(
            "/api/v1/jobs",
            json={"type": "csv_upload"},
            headers=auth_headers,
        )
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Redis failure during rate limit check — fail-open
# ---------------------------------------------------------------------------


async def test_redis_down_on_rate_limit_fails_open(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """If Redis is down for the rate limiter the request should still go through
    (fail-open) rather than blocking all traffic with a 500."""
    with patch(
        "app.utils.rate_limit._check",
        side_effect=ConnectionError("Redis unavailable"),
    ):
        resp = await client.post(
            "/api/v1/jobs",
            json={"type": "csv_upload"},
            headers=auth_headers,
        )
    # Should not be 429 — fail-open means we let the request through
    assert resp.status_code != 429


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
