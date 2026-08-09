"""API contract tests for /api/v1/sagas."""

from httpx import AsyncClient


async def test_create_saga_returns_chained_jobs(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/api/v1/sagas",
        json={
            "name": "import-pipeline",
            "steps": [
                {"type": "csv_upload"},
                {"type": "bulk_api_sync"},
                {"type": "report_gen"},
            ],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "running"
    assert body["name"] == "import-pipeline"
    assert len(body["steps"]) == 3

    # The first step is dispatched immediately; the rest wait on the chain.
    statuses = [s["status"] for s in body["steps"]]
    assert statuses[0] == "pending"
    assert statuses[1] == "waiting"
    assert statuses[2] == "waiting"

    # All steps share the saga_id.
    saga_ids = {s["saga_id"] for s in body["steps"]}
    assert saga_ids == {body["id"]}


async def test_create_saga_with_no_steps_rejected(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/api/v1/sagas",
        json={"name": "empty", "steps": []},
        headers=auth_headers,
    )
    assert resp.status_code == 422


async def test_get_saga_returns_steps(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    create_resp = await client.post(
        "/api/v1/sagas",
        json={
            "name": "two-step",
            "steps": [{"type": "csv_upload"}, {"type": "report_gen"}],
        },
        headers=auth_headers,
    )
    saga_id = create_resp.json()["id"]

    get_resp = await client.get(f"/api/v1/sagas/{saga_id}", headers=auth_headers)
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["id"] == saga_id
    assert len(body["steps"]) == 2


async def test_job_with_dependencies_starts_waiting(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    # First job — runs straight away.
    parent_resp = await client.post(
        "/api/v1/jobs", json={"type": "csv_upload"}, headers=auth_headers
    )
    parent_id = parent_resp.json()["id"]

    # Child depends on parent — parent is still PENDING (no worker in tests).
    child_resp = await client.post(
        "/api/v1/jobs",
        json={"type": "report_gen", "dependencies": [parent_id]},
        headers=auth_headers,
    )
    assert child_resp.status_code == 201
    assert child_resp.json()["status"] == "waiting"


async def test_job_with_missing_dependency_returns_404(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    import uuid as _uuid

    resp = await client.post(
        "/api/v1/jobs",
        json={"type": "report_gen", "dependencies": [str(_uuid.uuid4())]},
        headers=auth_headers,
    )
    assert resp.status_code == 404


async def test_list_sagas_returns_user_sagas(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    await client.post(
        "/api/v1/sagas",
        json={"name": "alpha", "steps": [{"type": "csv_upload"}]},
        headers=auth_headers,
    )
    await client.post(
        "/api/v1/sagas",
        json={
            "name": "beta",
            "steps": [{"type": "csv_upload"}, {"type": "report_gen"}],
        },
        headers=auth_headers,
    )

    resp = await client.get("/api/v1/sagas", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 2
    # Most recent first.
    names = [i["name"] for i in body["items"][:2]]
    assert "beta" in names and "alpha" in names
    beta = next(i for i in body["items"] if i["name"] == "beta")
    assert beta["step_count"] == 2


# ---------------------------------------------------------------------------
# Processor payload bounds on the saga surface (WO-P4-04 / E1-05)
#
# SagaStepRequest never goes through JobCreate, so bounds enforced only on
# JobCreate would be trivially bypassable via POST /sagas.
# ---------------------------------------------------------------------------


async def test_create_saga_rejects_oversized_step_payload(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/api/v1/sagas",
        json={
            "name": "bypass-attempt",
            "steps": [
                {"type": "csv_upload"},
                {"type": "doc_analysis", "payload": {"page_count": 10**9}},
            ],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert "page_count" in resp.text


async def test_create_saga_allows_compensation_step_types(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Non-JobType step types (e.g. `.compensate`) have no bound model — no-op."""
    resp = await client.post(
        "/api/v1/sagas",
        json={
            "name": "compensating",
            "steps": [{"type": "csv_upload.compensate", "payload": {"anything": 1}}],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
