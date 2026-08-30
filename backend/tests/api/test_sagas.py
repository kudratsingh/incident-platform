"""API contract tests for /api/v1/sagas."""

import uuid

from app.api.sagas import MAX_SAGA_STEPS
from app.models.enums import JobStatus
from app.models.job import Job
from app.models.user import User
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


# ---------------------------------------------------------------------------
# Admission control (WO-R2-12)
#
# `POST /sagas` creates N job rows. It used to create them with no per-IP rate
# limit, no backpressure check and no tenant quota check, while `POST /jobs`
# applied all three — so the per-tenant monthly quota was unenforceable rather
# than merely leaky: `_check_monthly_quota` counts every `jobs` row, so
# saga-created steps consumed the cap that blocks `POST /jobs` while the saga
# endpoint itself was never blocked.
#
# The three checks now live behind one shared guard (`utils/admission.py`)
# that both endpoints call, so a future job-creating surface inherits them
# instead of having to remember three imports in the right order.
#
# Backpressure coverage for this endpoint lives in tests/api/test_failure_modes.py,
# next to the `POST /jobs` fail-open tests it has to match.
# ---------------------------------------------------------------------------


async def _fill_quota(  # type: ignore[no-untyped-def]
    db_session, tenant, user, *, cap: int, used: int
) -> None:
    """Set the tenant's monthly cap and burn `used` of it with real job rows."""
    tenant.rate_limit_per_minute = 0
    tenant.quota_jobs_per_month = cap
    for _ in range(used):
        db_session.add(
            Job(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                user_id=user.id,
                type="csv_upload",
                status=JobStatus.PENDING,
                priority=0,
                payload={},
            )
        )
    await db_session.flush()


async def test_saga_is_refused_when_the_tenant_is_at_its_monthly_quota(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    default_tenant,  # type: ignore[no-untyped-def]
    test_user: User,
    auth_headers: dict[str, str],
) -> None:
    """THE WO-R2-12 assertion: the billing cap binds on this endpoint too.

    Pre-fix this returned 201 and created another job row for a tenant whose
    `POST /jobs` was already being refused — the cap was real on one surface
    and decorative on the other, which makes it unenforceable overall.
    """
    await _fill_quota(db_session, default_tenant, test_user, cap=2, used=2)

    resp = await client.post(
        "/api/v1/sagas",
        json={"name": "over-quota", "steps": [{"type": "csv_upload"}]},
        headers=auth_headers,
    )

    assert resp.status_code == 429, resp.text
    assert resp.json()["error_code"] == "quota_exceeded"


async def test_a_saga_counts_as_its_steps_against_the_quota(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    default_tenant,  # type: ignore[no-untyped-def]
    test_user: User,
    auth_headers: dict[str, str],
) -> None:
    """The counting decision: N steps are N jobs, checked before any insert.

    The tenant is UNDER its cap (8 of 10), so a single `POST /jobs` would
    succeed here — but this saga creates five rows and would land at 13.
    Counting the saga as one unit would let it through and overshoot by
    three; counting it as its steps refuses it up front, which also stops a
    saga from committing part of its chain before meeting the cap mid-loop.
    """
    await _fill_quota(db_session, default_tenant, test_user, cap=10, used=8)

    resp = await client.post(
        "/api/v1/sagas",
        json={"name": "five-steps", "steps": [{"type": "csv_upload"}] * 5},
        headers=auth_headers,
    )

    assert resp.status_code == 429, resp.text
    body = resp.json()
    assert body["error_code"] == "quota_exceeded"
    assert body["details"] == {"limit": 10, "used": 8, "requested": 5}


async def test_a_saga_that_fits_in_the_remaining_quota_still_succeeds(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    default_tenant,  # type: ignore[no-untyped-def]
    test_user: User,
    auth_headers: dict[str, str],
) -> None:
    """The cap must bind, not block: the request that exactly fits is allowed.

    Guards the fencepost that would turn this fix into a denial of service —
    8 used + 2 steps against a cap of 10 is the last saga that fits.
    """
    await _fill_quota(db_session, default_tenant, test_user, cap=10, used=8)

    resp = await client.post(
        "/api/v1/sagas",
        json={"name": "exactly-fits", "steps": [{"type": "csv_upload"}] * 2},
        headers=auth_headers,
    )

    assert resp.status_code == 201, resp.text
    assert len(resp.json()["steps"]) == 2


async def test_saga_step_count_is_bounded(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """One request cannot create an unbounded number of job rows."""
    resp = await client.post(
        "/api/v1/sagas",
        json={
            "name": "too-many",
            "steps": [{"type": "csv_upload"}] * (MAX_SAGA_STEPS + 1),
        },
        headers=auth_headers,
    )

    assert resp.status_code == 422
    assert "steps" in resp.text


async def test_a_saga_at_the_step_limit_is_accepted(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    default_tenant,  # type: ignore[no-untyped-def]
    auth_headers: dict[str, str],
) -> None:
    """The bound is a limit, not an off-by-one refusal."""
    default_tenant.quota_jobs_per_month = 0  # quota disabled — bound only
    await db_session.flush()

    resp = await client.post(
        "/api/v1/sagas",
        json={
            "name": "at-the-limit",
            "steps": [{"type": "csv_upload"}] * MAX_SAGA_STEPS,
        },
        headers=auth_headers,
    )

    assert resp.status_code == 201, resp.text
    assert len(resp.json()["steps"]) == MAX_SAGA_STEPS
