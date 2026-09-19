"""The operator half of ADR 0035, over REST (WO-R3-312).

Four readings a human could not get over HTTP before — the responder's run, consumer
lag, breaker state and alerts including resolved ones — plus the DLQ fields the job
shape was missing. The rules under test are the ones a console depends on: `support`
may read all of it, a `user` may read none of it, one tenant never sees another's runs,
and an unknown reading says **why** it is unknown instead of reading as a healthy zero.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from app.core.breaker_state import breaker_key_for
from app.core.consumer_lag import LIVE_REFRESHED_GROUP, lag_key, samples_key
from app.core.security import create_access_token, hash_password
from app.dependencies import get_db, get_redis
from app.main import create_app
from app.models.agent_run import AgentRun
from app.models.alert import Alert
from app.models.enums import JobStatus, JobType, UserRole
from app.models.job import Job
from app.models.tenant import Tenant
from app.models.user import User
from app.repositories.triage import TriageRepository
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime(2026, 9, 19, 7, 0, tzinfo=UTC)


class _RedisStub:
    """Just enough Redis for the two cache-backed readings."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.store[key] = value
        return True

    async def scan(
        self, cursor: int, match: str = "*", count: int = 100
    ) -> tuple[int, list[str]]:
        prefix = match.rstrip("*")
        return 0, [k for k in self.store if k.startswith(prefix)]

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self.store.get(k) for k in keys]

    async def delete(self, *keys: str) -> int:
        return 0

    async def incr(self, key: str) -> int:
        return 1

    async def expire(self, key: str, seconds: int) -> bool:
        return True


@pytest_asyncio.fixture
async def redis_stub() -> _RedisStub:
    return _RedisStub()


@pytest_asyncio.fixture
async def app_client(  # type: ignore[no-untyped-def]
    db_session: AsyncSession, default_tenant, redis_stub: _RedisStub
):
    app = create_app()

    async def _override_db():  # type: ignore[no-untyped-def]
        yield db_session

    async def _override_redis():  # type: ignore[no-untyped-def]
        yield redis_stub

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        yield ac


async def _user(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
    role: UserRole,
    *,
    platform_admin: bool = False,
) -> User:
    user = User(
        tenant_id=tenant_id,
        email=f"{role.value}-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password=hash_password("password123"),
        role=role,
        is_active=True,
        is_platform_admin=platform_admin,
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    return user


def _headers(user: User) -> dict[str, str]:
    token = create_access_token(
        {"sub": str(user.id), "tenant_id": str(user.tenant_id), "role": user.role}
    )
    return {"Authorization": f"Bearer {token}"}


async def _service_account_id(db_session: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    from app.models.service_account import ServiceAccount

    sa = ServiceAccount(
        tenant_id=tenant_id,
        name=f"reporter-{uuid.uuid4().hex[:8]}",
        scopes=["agent_runs:write"],
        is_active=True,
    )
    db_session.add(sa)
    await db_session.flush()
    return sa.id


async def _run(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    state: str = "investigating",
    finished: bool = False,
    alert_id: uuid.UUID | None = None,
    briefing: dict[str, Any] | None = None,
) -> AgentRun:
    run = AgentRun(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        alert_id=alert_id,
        service_account_id=await _service_account_id(db_session, tenant_id),
        scenario="consumer-outage",
        state=state,
        phase_history=[
            {"state": "triage", "at": _NOW.isoformat()},
            {"state": state, "at": (_NOW + timedelta(seconds=5)).isoformat()},
        ],
        current_hypothesis={"name": "a stalled consumer", "confidence": 0.8},
        last_step={"kind": "read", "tool": "get_consumer_lag"},
        briefing=briefing,
        finished_at=(_NOW + timedelta(minutes=2)) if finished else None,
    )
    db_session.add(run)
    await db_session.flush()
    await db_session.refresh(run)
    return run


# --------------------------------------------------------------------------
# GET /admin/agent-runs
# --------------------------------------------------------------------------


async def test_support_reads_the_active_run_with_its_phase_history(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    run = await _run(db_session, default_tenant.id)
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)

    resp = await app_client.get(
        "/api/v1/admin/agent-runs?active=true", headers=_headers(support)
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == str(run.id)
    assert item["state"] == "investigating"
    assert item["active"] is True
    assert [e["state"] for e in item["phase_history"]] == ["triage", "investigating"]
    assert item["current_hypothesis"]["name"] == "a stalled consumer"
    assert item["last_step"]["tool"] == "get_consumer_lag"
    assert item["briefing"] is None
    assert item["scenario"] == "consumer-outage"


async def test_the_active_filter_excludes_a_finished_run(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    await _run(db_session, default_tenant.id, state="resolved", finished=True)
    open_run = await _run(db_session, default_tenant.id)
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)

    active = (
        await app_client.get(
            "/api/v1/admin/agent-runs?active=true", headers=_headers(support)
        )
    ).json()
    both = (
        await app_client.get("/api/v1/admin/agent-runs", headers=_headers(support))
    ).json()

    assert [i["id"] for i in active["items"]] == [str(open_run.id)]
    assert both["total"] == 2
    assert {i["active"] for i in both["items"]} == {True, False}


async def test_runs_can_be_narrowed_to_one_alert(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    alert = Alert(
        tenant_id=default_tenant.id,
        severity="critical",
        source="slo:job_completion",
        title="Job completion burning budget",
    )
    db_session.add(alert)
    await db_session.flush()
    matching = await _run(db_session, default_tenant.id, alert_id=alert.id)
    await _run(db_session, default_tenant.id)
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)

    resp = await app_client.get(
        f"/api/v1/admin/agent-runs?alert_id={alert.id}", headers=_headers(support)
    )

    assert [i["id"] for i in resp.json()["items"]] == [str(matching.id)]


async def test_one_run_by_id_carries_its_briefing(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    run = await _run(
        db_session,
        default_tenant.id,
        state="escalated",
        finished=True,
        briefing={
            "final_state": "escalated",
            "escalation_reason": "budget spent",
            "prose": "Lag never drained.",
        },
    )
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)

    resp = await app_client.get(
        f"/api/v1/admin/agent-runs/{run.id}", headers=_headers(support)
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["briefing"]["escalation_reason"] == "budget spent"
    assert body["briefing"]["prose"] == "Lag never drained."
    assert body["active"] is False


async def test_another_tenants_run_is_a_404_not_a_403(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    """The id space stays opaque: "not yours" and "not there" answer the same, exactly
    as `JobRepository.get_for_tenant` keeps them. RLS is the backstop and is proved on
    a real Postgres in the integration tier."""
    other = Tenant(
        id=uuid.uuid4(), slug=f"other-{uuid.uuid4().hex[:6]}", name="Other", is_active=True
    )
    db_session.add(other)
    await db_session.flush()
    foreign = await _run(db_session, other.id)
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)

    resp = await app_client.get(
        f"/api/v1/admin/agent-runs/{foreign.id}", headers=_headers(support)
    )
    listing = await app_client.get(
        "/api/v1/admin/agent-runs", headers=_headers(support)
    )

    assert resp.status_code == 404
    assert listing.json()["total"] == 0


async def test_a_platform_admin_may_cross_tenants_and_a_plain_admin_may_not(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    other = Tenant(
        id=uuid.uuid4(), slug=f"other-{uuid.uuid4().hex[:6]}", name="Other", is_active=True
    )
    db_session.add(other)
    await db_session.flush()
    foreign = await _run(db_session, other.id)
    platform = await _user(
        db_session, default_tenant.id, UserRole.ADMIN, platform_admin=True
    )
    plain = await _user(db_session, default_tenant.id, UserRole.ADMIN)

    crossed = await app_client.get(
        f"/api/v1/admin/agent-runs?tenant_id={other.id}", headers=_headers(platform)
    )
    refused = await app_client.get(
        f"/api/v1/admin/agent-runs?tenant_id={other.id}", headers=_headers(plain)
    )

    assert [i["id"] for i in crossed.json()["items"]] == [str(foreign.id)]
    # Not an error — the override is simply ignored, which is `resolve_admin_tenant`'s
    # documented behaviour for a non-platform admin.
    assert refused.json()["total"] == 0


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/admin/agent-runs",
        "/api/v1/admin/consumer-lag",
        "/api/v1/admin/circuit-breakers",
        "/api/v1/admin/alerts",
    ],
)
async def test_a_plain_user_may_read_none_of_it(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant, path: str
) -> None:
    """Operator-only is the whole point of the split in ADR 0035."""
    plain = await _user(db_session, default_tenant.id, UserRole.USER)

    resp = await app_client.get(path, headers=_headers(plain))

    assert resp.status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/admin/agent-runs",
        "/api/v1/admin/consumer-lag",
        "/api/v1/admin/circuit-breakers",
        "/api/v1/admin/alerts",
    ],
)
async def test_no_token_is_a_401(app_client: AsyncClient, path: str) -> None:
    resp = await app_client.get(path)

    assert resp.status_code == 401


# --------------------------------------------------------------------------
# GET /admin/consumer-lag
# --------------------------------------------------------------------------


async def test_consumer_lag_reports_every_group_with_the_live_one_named(
    app_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,
    redis_stub: _RedisStub,
) -> None:
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)
    redis_stub.store[lag_key(LIVE_REFRESHED_GROUP)] = "42"
    redis_stub.store[samples_key(LIVE_REFRESHED_GROUP)] = (
        '[{"lag": 42, "measured_at": "2026-09-19T07:00:00+00:00"},'
        ' {"lag": 30, "measured_at": "2026-09-19T06:59:00+00:00"}]'
    )
    redis_stub.store[lag_key("billing-consumer")] = "7"

    body = (
        await app_client.get("/api/v1/admin/consumer-lag", headers=_headers(support))
    ).json()

    assert body["live_group"] == LIVE_REFRESHED_GROUP
    assert body["total"] == len(body["groups"]) == 8
    by_name = {g["consumer_group"]: g for g in body["groups"]}
    live = by_name[LIVE_REFRESHED_GROUP]
    assert live["lag"] == 42
    assert live["lag_known"] is True
    assert live["source"] == "live"
    assert live["lag_unknown_reason"] is None
    assert live["measured_at"] is not None
    assert [s["lag"] for s in live["recent_samples"]] == [42, 30]
    static = by_name["billing-consumer"]
    assert (static["lag"], static["source"]) == (7, "static")
    # A constant was never measured at a moment, so it carries no time and no history.
    assert static["measured_at"] is None
    assert static["recent_samples"] == []


async def test_an_absent_lag_is_null_with_a_reason_never_a_zero(
    app_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,
    redis_stub: _RedisStub,
) -> None:
    """THE rule the order set for every new field: an unknown reading says why, in
    words the operator can act on, and the two reasons are different jobs."""
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)

    body = (
        await app_client.get("/api/v1/admin/consumer-lag", headers=_headers(support))
    ).json()

    by_name = {g["consumer_group"]: g for g in body["groups"]}
    live = by_name[LIVE_REFRESHED_GROUP]
    assert live["lag"] is None
    assert live["lag_known"] is False
    assert "metrics loop" in live["lag_unknown_reason"]
    static = by_name["orders-consumer"]
    assert static["lag"] is None
    assert "environment problem" in static["lag_unknown_reason"]
    # No zero anywhere in the reply.
    assert all(g["lag"] is None for g in body["groups"])


async def test_the_console_and_the_agent_read_one_number(
    app_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,
    redis_stub: _RedisStub,
) -> None:
    """Both surfaces call `app/core/consumer_lag.read_lag`, so the console cannot show
    a lag the agent's tool would call unknown, or the reverse."""
    from app.dependencies import Principal
    from app.mcp.registry import ToolContext
    from app.mcp.tools.consumer_lag import GetConsumerLagInput, get_consumer_lag

    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)
    redis_stub.store[lag_key(LIVE_REFRESHED_GROUP)] = "29"

    rest = (
        await app_client.get("/api/v1/admin/consumer-lag", headers=_headers(support))
    ).json()
    tool = await get_consumer_lag(
        GetConsumerLagInput(consumer_group=LIVE_REFRESHED_GROUP),
        ToolContext(
            db=db_session,
            redis=redis_stub,  # type: ignore[arg-type]
            principal=Principal(kind="user", tenant_id=default_tenant.id),
        ),
    )

    live = next(
        g for g in rest["groups"] if g["consumer_group"] == LIVE_REFRESHED_GROUP
    )
    assert (live["lag"], live["lag_known"], live["source"]) == (
        tool.lag,
        tool.lag_known,
        tool.source,
    )


# --------------------------------------------------------------------------
# GET /admin/circuit-breakers
# --------------------------------------------------------------------------


async def test_circuit_breakers_reports_a_published_record(
    app_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,
    redis_stub: _RedisStub,
) -> None:
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)
    redis_stub.store[breaker_key_for("bulk-api-sync")] = (
        '{"name": "bulk-api-sync", "state": "open", "failure_count": 3,'
        ' "failure_threshold": 3, "recovery_timeout_s": 30.0,'
        ' "last_state_change_at": "2026-09-19T06:59:00+00:00",'
        ' "last_failure_at": "2026-09-19T06:59:00+00:00",'
        ' "last_failure_reason_class": "timeout",'
        ' "recorded_at": "2026-09-19T06:59:30+00:00"}'
    )

    body = (
        await app_client.get(
            "/api/v1/admin/circuit-breakers", headers=_headers(support)
        )
    ).json()

    assert body["total"] == 1
    breaker = body["breakers"][0]
    assert breaker["name"] == "bulk-api-sync"
    assert breaker["state"] == "open"
    assert breaker["failure_count"] == 3
    assert breaker["last_failure_reason_class"] == "timeout"
    assert breaker["reported_age_s"] >= 0
    assert breaker["seconds_since_state_change"] >= 0
    assert body["unknown_reason"] is None


async def test_no_breaker_record_is_an_empty_list_not_a_closed_breaker(
    app_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,
    redis_stub: _RedisStub,
) -> None:
    """ADR 0030's rule, carried onto the console: absent is unknown, never closed."""
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)

    body = (
        await app_client.get(
            "/api/v1/admin/circuit-breakers", headers=_headers(support)
        )
    ).json()

    assert body["breakers"] == []
    assert body["total"] == 0
    # And the empty list says why it is empty, so a console cannot render it as "all
    # breakers healthy".
    assert body["unknown_reason"]


# --------------------------------------------------------------------------
# GET /admin/alerts
# --------------------------------------------------------------------------


async def test_alerts_shows_resolved_ones_too_unlike_the_agents_tool(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    """The one thing `list_active_alerts` never returns, and the thing a human reading
    a timeline after the fact needs."""
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)
    active = Alert(
        tenant_id=default_tenant.id,
        severity="critical",
        source="slo:job_completion",
        title="Still burning",
    )
    resolved = Alert(
        tenant_id=default_tenant.id,
        severity="warning",
        source="dlq:threshold",
        title="Cleared",
        resolved_at=_NOW,
    )
    db_session.add_all([active, resolved])
    await db_session.flush()

    both = (
        await app_client.get("/api/v1/admin/alerts", headers=_headers(support))
    ).json()
    only_active = (
        await app_client.get(
            "/api/v1/admin/alerts?active=true", headers=_headers(support)
        )
    ).json()
    only_resolved = (
        await app_client.get(
            "/api/v1/admin/alerts?active=false", headers=_headers(support)
        )
    ).json()

    assert both["total"] == 2
    assert [a["title"] for a in only_active["items"]] == ["Still burning"]
    assert [a["title"] for a in only_resolved["items"]] == ["Cleared"]
    assert only_resolved["items"][0]["resolved_at"] is not None


async def test_alerts_are_tenant_scoped(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    other = Tenant(
        id=uuid.uuid4(), slug=f"other-{uuid.uuid4().hex[:6]}", name="Other", is_active=True
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        Alert(
            tenant_id=other.id,
            severity="critical",
            source="slo:job_completion",
            title="Not yours",
        )
    )
    await db_session.flush()
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)

    body = (
        await app_client.get("/api/v1/admin/alerts", headers=_headers(support))
    ).json()

    assert body["total"] == 0


# --------------------------------------------------------------------------
# The widened job shape
# --------------------------------------------------------------------------


async def _dead_lettered_job(
    db_session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> Job:
    job = Job(
        tenant_id=tenant_id,
        user_id=user_id,
        type=JobType.CSV_UPLOAD.value,
        status=JobStatus.DEAD_LETTER.value,
        payload={},
        error_message="SchemaValidationError: payload missing required field",
        retry_count=3,
        remediation_hint="human_required",
        fenced_at=_NOW,
        fenced_by="service_account:0f9a",
        completed_at=_NOW,
    )
    db_session.add(job)
    await db_session.flush()
    await db_session.refresh(job)
    return job


async def test_the_job_shape_carries_the_dlq_fields_and_the_triage_row(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)
    job = await _dead_lettered_job(db_session, default_tenant.id, support.id)
    await TriageRepository(db_session).upsert(
        job_id=job.id,
        tenant_id=default_tenant.id,
        root_cause_category="bad_data",
        summary="The payload is missing a required field.",
        suggested_fix="Correct the producer.",
        is_retryable=False,
        confidence=0.9,
        model_used="test-model",
        usage=None,
    )

    listing = (
        await app_client.get(
            "/api/v1/admin/jobs?status=dead_letter", headers=_headers(support)
        )
    ).json()
    single = (
        await app_client.get(
            f"/api/v1/admin/jobs/{job.id}", headers=_headers(support)
        )
    ).json()

    for body in (listing["items"][0], single):
        assert body["remediation_hint"] == "human_required"
        assert body["fenced_at"] is not None
        assert body["fenced_by"] == "service_account:0f9a"
        assert body["dead_lettered_at"] is not None
        assert body["triage"]["root_cause_category"] == "bad_data"
        assert body["triage"]["is_retryable"] is False
        assert body["triage"]["confidence"] == 0.9


async def test_the_new_fields_are_null_for_a_job_that_is_not_dead_lettered(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    """Additive and nullable: `status` is the reason every one of them is null, which
    is why none of them carries a reason string of its own."""
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)
    job = Job(
        tenant_id=default_tenant.id,
        user_id=support.id,
        type=JobType.CSV_UPLOAD.value,
        status=JobStatus.COMPLETED.value,
        payload={},
        completed_at=_NOW,
    )
    db_session.add(job)
    await db_session.flush()

    body = (
        await app_client.get(
            f"/api/v1/admin/jobs/{job.id}", headers=_headers(support)
        )
    ).json()

    assert body["dead_lettered_at"] is None
    assert body["remediation_hint"] is None
    assert body["fenced_at"] is None
    assert body["fenced_by"] is None
    assert body["triage"] is None
    # `completed_at` is set and is NOT the dead-letter time: one clock per fact.
    assert body["completed_at"] is not None


async def test_a_dead_lettered_job_with_no_triage_row_keeps_a_null_triage(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    """The normal case — the triage consumer is off by default — so the console must
    render a row with no analysis rather than wait for one."""
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)
    job = await _dead_lettered_job(db_session, default_tenant.id, support.id)

    body = (
        await app_client.get(
            f"/api/v1/admin/jobs/{job.id}", headers=_headers(support)
        )
    ).json()

    assert body["triage"] is None
    assert body["dead_lettered_at"] is not None
