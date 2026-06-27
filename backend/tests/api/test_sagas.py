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
