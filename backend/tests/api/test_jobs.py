"""API contract tests for /api/v1/jobs endpoints."""

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


async def test_admin_dlq_stats_empty(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    resp = await client.get("/api/v1/admin/dlq/stats", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"total": 0, "by_type": {}}


async def test_admin_dlq_stats_counts_by_type(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    admin_user,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
) -> None:
    """Insert two dead-lettered jobs of different types and assert the breakdown."""
    from app.models.enums import JobStatus, JobType
    from app.models.job import Job

    for jt in (JobType.CSV_UPLOAD, JobType.CSV_UPLOAD, JobType.BULK_API_SYNC):
        db_session.add(
            Job(
                user_id=admin_user.id,
                type=jt,
                status=JobStatus.DEAD_LETTER,
                retry_count=3,
                max_retries=3,
                priority=0,
                error_message="boom",
            )
        )
    await db_session.flush()

    resp = await client.get("/api/v1/admin/dlq/stats", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert body["by_type"] == {
        JobType.CSV_UPLOAD: 2,
        JobType.BULK_API_SYNC: 1,
    }


async def test_admin_job_timeline_returns_events_in_order(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    admin_user,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
) -> None:
    """Event-sourcing timeline endpoint replays job_events ordered by recorded_at."""
    import uuid as _uuid

    from app.models.event_log import JobEvent

    job_id = _uuid.uuid4()
    for i, name in enumerate(["job.submitted", "job.progress", "job.completed"]):
        db_session.add(
            JobEvent(
                job_id=job_id,
                event_name=name,
                payload={"event": name, "job_id": str(job_id)},
                kafka_topic=name,
                kafka_partition=0,
                kafka_offset=i,
            )
        )
    await db_session.flush()

    resp = await client.get(
        f"/api/v1/admin/jobs/{job_id}/timeline", headers=admin_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3
    assert [e["event_name"] for e in body["events"]] == [
        "job.submitted",
        "job.progress",
        "job.completed",
    ]
    assert [e["kafka_offset"] for e in body["events"]] == [0, 1, 2]


async def test_admin_stats_reads_from_read_model(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """/admin/stats reads denormalized Redis sets, not the jobs table."""
    resp = await client.get("/api/v1/admin/stats", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "by_status" in body
    # Mocked Redis in tests returns 0 cardinality.
    assert set(body["by_status"].keys()) == {
        "running",
        "completed",
        "failed",
        "dead_letter",
    }


async def test_admin_user_stats_uses_user_specific_keys(
    client: AsyncClient,
    admin_user,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
) -> None:
    resp = await client.get(
        f"/api/v1/admin/users/{admin_user.id}/stats", headers=admin_headers
    )
    assert resp.status_code == 200
    assert "by_status" in resp.json()


async def test_admin_slos_returns_two_objectives(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """The two declared SLOs are returned; with no traffic, both are healthy."""
    resp = await client.get("/api/v1/admin/slos", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    ids = {s["id"] for s in body["slos"]}
    assert ids == {"job_completion_rate", "job_dispatch_latency"}
    for s in body["slos"]:
        assert s["healthy"] is True
        assert s["runbook_id"].startswith("rb-")


async def test_admin_slos_reflects_dead_letter_failures(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    admin_user,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
) -> None:
    """Insert 8 completed + 2 dead-lettered → 80% success → completion SLO breached."""
    from app.models.enums import JobStatus, JobType
    from app.models.job import Job

    for _ in range(8):
        db_session.add(
            Job(
                user_id=admin_user.id,
                type=JobType.CSV_UPLOAD,
                status=JobStatus.COMPLETED,
                retry_count=0,
                max_retries=3,
                priority=0,
            )
        )
    for _ in range(2):
        db_session.add(
            Job(
                user_id=admin_user.id,
                type=JobType.CSV_UPLOAD,
                status=JobStatus.DEAD_LETTER,
                retry_count=3,
                max_retries=3,
                priority=0,
                error_message="boom",
            )
        )
    await db_session.flush()

    resp = await client.get("/api/v1/admin/slos", headers=admin_headers)
    assert resp.status_code == 200
    completion = next(
        s for s in resp.json()["slos"] if s["id"] == "job_completion_rate"
    )
    assert completion["total"] == 10
    assert completion["failed"] == 2
    assert abs(completion["current"] - 0.8) < 1e-9
    assert completion["healthy"] is False
    # Failure rate 20% vs allowed 1% → burn rate 20×.
    assert completion["burn_rate"] is not None
    assert abs(completion["burn_rate"] - 20.0) < 1e-6


async def test_admin_runbooks_list_and_get(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    resp = await client.get("/api/v1/admin/runbooks", headers=admin_headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert any(rb["id"] == "rb-slo-job-completion" for rb in items)

    detail = await client.get(
        "/api/v1/admin/runbooks/rb-slo-job-completion", headers=admin_headers
    )
    assert detail.status_code == 200
    assert detail.json()["id"] == "rb-slo-job-completion"

    missing = await client.get("/api/v1/admin/runbooks/nope", headers=admin_headers)
    assert missing.status_code == 404


async def test_admin_replay_resets_retry_count(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    admin_user,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
) -> None:
    """A DLQ'd job at max retries replays with retry_count=0 and a clean error."""
    from app.models.enums import JobStatus, JobType
    from app.models.job import Job

    job = Job(
        user_id=admin_user.id,
        type=JobType.CSV_UPLOAD,
        status=JobStatus.DEAD_LETTER,
        retry_count=3,
        max_retries=3,
        priority=0,
        error_message="last attempt boom",
    )
    db_session.add(job)
    await db_session.flush()
    await db_session.refresh(job)

    resp = await client.post(
        f"/api/v1/admin/jobs/{job.id}/replay", headers=admin_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["retry_count"] == 0
    assert body["error_message"] is None
