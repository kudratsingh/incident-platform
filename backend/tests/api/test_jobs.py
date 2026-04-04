"""API contract tests for /api/v1/jobs endpoints."""

import pytest
from httpx import AsyncClient


async def test_create_job_returns_201(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/api/v1/jobs",
        json={"type": "csv_upload", "payload": {"filename": "data.csv"}},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["type"] == "csv_upload"
    assert body["status"] == "pending"
    assert "id" in body
    assert "user_id" in body


async def test_create_job_unauthenticated_returns_401(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/jobs", json={"type": "csv_upload"}
    )
    assert resp.status_code == 401


async def test_create_job_invalid_type_returns_422(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/api/v1/jobs",
        json={"type": "not_a_real_type"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


async def test_create_job_idempotency(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    payload = {"type": "report_gen", "idempotency_key": "unique-key-abc"}
    resp1 = await client.post("/api/v1/jobs", json=payload, headers=auth_headers)
    resp2 = await client.post("/api/v1/jobs", json=payload, headers=auth_headers)
    assert resp1.status_code == 201
    assert resp2.status_code == 201
    assert resp1.json()["id"] == resp2.json()["id"]


async def test_list_jobs_returns_paginated(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    # Create a couple of jobs
    for job_type in ("csv_upload", "report_gen"):
        await client.post(
            "/api/v1/jobs", json={"type": job_type}, headers=auth_headers
        )

    resp = await client.get("/api/v1/jobs", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert "total" in body
    assert "page" in body
    assert isinstance(body["items"], list)


async def test_get_job_by_id(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    create_resp = await client.post(
        "/api/v1/jobs", json={"type": "doc_analysis"}, headers=auth_headers
    )
    job_id = create_resp.json()["id"]

    resp = await client.get(f"/api/v1/jobs/{job_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == job_id


async def test_get_job_not_found_returns_404(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    fake_id = "00000000-0000-0000-0000-000000000000"
    resp = await client.get(f"/api/v1/jobs/{fake_id}", headers=auth_headers)
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "not_found"


async def test_user_cannot_see_other_users_job(
    client: AsyncClient,
    auth_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    # Admin creates a job
    create_resp = await client.post(
        "/api/v1/jobs", json={"type": "csv_upload"}, headers=admin_headers
    )
    job_id = create_resp.json()["id"]

    # Regular user tries to fetch it
    resp = await client.get(f"/api/v1/jobs/{job_id}", headers=auth_headers)
    assert resp.status_code == 403


async def test_admin_can_list_all_jobs(
    client: AsyncClient,
    auth_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    await client.post(
        "/api/v1/jobs", json={"type": "csv_upload"}, headers=auth_headers
    )
    resp = await client.get("/api/v1/admin/jobs", headers=admin_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json()["items"], list)
