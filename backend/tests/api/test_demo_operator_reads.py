"""The operator half of ADR 0035, over REST (WO-R3-312).

Four readings a human could not get over HTTP before — the responder's run, consumer
lag, breaker state and alerts including resolved ones — plus the DLQ fields the job
shape was missing. The rules under test are the ones a console depends on: `support`
may read all of it, a `user` may read none of it, one tenant never sees another's runs,
and an unknown reading says **why** it is unknown instead of reading as a healthy zero.
"""

from __future__ import annotations

import json
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


# --------------------------------------------------------------------------
# GET /admin/agent-runs/{id}/steps (WO-R3-328, ADR 0037)
# --------------------------------------------------------------------------


def _ledger(count: int, *, start: int = 1) -> list[dict[str, Any]]:
    return [
        {
            "seq": seq,
            "kind": "action" if seq % 3 == 0 else "read",
            "tool": "restart_consumer_group" if seq % 3 == 0 else "get_consumer_lag",
            "arguments": {"consumer_group": "worker-dispatcher"},
            "result_excerpt": f"reading {seq}",
            "outcome": "success",
            "latency_ms": float(seq),
            "at": (_NOW + timedelta(seconds=seq)).isoformat(),
        }
        for seq in range(start, start + count)
    ]


async def _run_with_steps(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    steps: list[dict[str, Any]],
    steps_dropped: int = 0,
    finished: bool = False,
) -> AgentRun:
    run = await _run(db_session, tenant_id, finished=finished)
    run.steps = steps
    run.steps_dropped = steps_dropped
    run.hypotheses = [
        {"name": "a stalled consumer", "confidence": 0.8, "reasoning_excerpt": "lag up"}
    ]
    run.plan = {"action_tool": "restart_consumer_group"}
    run.verification = {"verdict": "verified", "attempt": 1}
    run.verifications = [{"verdict": "verified", "attempt": 1}]
    run.budget = {"tool_calls_used": 5, "tool_calls_max": 13}
    await db_session.flush()
    await db_session.refresh(run)
    return run


async def test_the_whole_ledger_comes_back_oldest_first(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    run = await _run_with_steps(db_session, default_tenant.id, steps=_ledger(4))
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)

    body = (
        await app_client.get(
            f"/api/v1/admin/agent-runs/{run.id}/steps", headers=_headers(support)
        )
    ).json()

    assert [s["seq"] for s in body["steps"]] == [1, 2, 3, 4]
    assert body["returned"] == body["total"] == 4
    assert body["steps_dropped"] == 0
    assert body["after_seq"] is None
    assert body["next_after_seq"] == 4
    assert body["state"] == "investigating"
    assert body["finished_at"] is None
    first = body["steps"][0]
    assert first["tool"] == "get_consumer_lag"
    assert first["arguments"] == {"consumer_group": "worker-dispatcher"}
    assert first["result_excerpt"] == "reading 1"
    assert first["outcome"] == "success"


async def test_after_seq_returns_only_what_is_new(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    """The polling contract: a console that has drawn up to `seq` 3 asks for 4 onwards
    and is never handed a step it has already shown."""
    run = await _run_with_steps(db_session, default_tenant.id, steps=_ledger(5))
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)

    body = (
        await app_client.get(
            f"/api/v1/admin/agent-runs/{run.id}/steps?after_seq=3",
            headers=_headers(support),
        )
    ).json()

    assert [s["seq"] for s in body["steps"]] == [4, 5]
    assert (body["returned"], body["total"]) == (2, 5)
    assert body["after_seq"] == 3
    assert body["next_after_seq"] == 5


async def test_a_poll_that_finds_nothing_new_keeps_the_cursor_moving(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    """An empty page must not hand back a cursor that re-reads the tail next tick."""
    run = await _run_with_steps(db_session, default_tenant.id, steps=_ledger(2))
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)

    body = (
        await app_client.get(
            f"/api/v1/admin/agent-runs/{run.id}/steps?after_seq=2",
            headers=_headers(support),
        )
    ).json()

    assert body["steps"] == []
    assert body["returned"] == 0
    assert body["next_after_seq"] == 2


async def test_the_ledger_is_sorted_by_seq_not_trusted_in_stored_order(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    """The row is appended in the order the responder reported; a report that arrived
    out of order would otherwise draw in the wrong place on a screen."""
    stored = _ledger(1, start=3) + _ledger(1, start=1) + _ledger(1, start=2)
    run = await _run_with_steps(db_session, default_tenant.id, steps=stored)
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)

    body = (
        await app_client.get(
            f"/api/v1/admin/agent-runs/{run.id}/steps", headers=_headers(support)
        )
    ).json()

    assert [s["seq"] for s in body["steps"]] == [1, 2, 3]


async def test_a_capped_ledger_says_what_it_dropped(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    """`total` is what is stored and `steps_dropped` is what is gone: a console showing
    the newest 200 calls as the whole run is the thing this field prevents."""
    run = await _run_with_steps(
        db_session, default_tenant.id, steps=_ledger(3, start=12), steps_dropped=11
    )
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)

    body = (
        await app_client.get(
            f"/api/v1/admin/agent-runs/{run.id}/steps", headers=_headers(support)
        )
    ).json()

    assert body["total"] == 3
    assert body["steps_dropped"] == 11
    assert [s["seq"] for s in body["steps"]] == [12, 13, 14]


async def test_a_run_with_no_steps_is_an_empty_ledger_not_a_404(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    run = await _run(db_session, default_tenant.id)
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)

    resp = await app_client.get(
        f"/api/v1/admin/agent-runs/{run.id}/steps", headers=_headers(support)
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["steps"] == []
    assert (body["total"], body["steps_dropped"]) == (0, 0)
    assert body["next_after_seq"] is None


async def test_another_tenants_ledger_is_a_404(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    other = Tenant(
        id=uuid.uuid4(), slug=f"other-{uuid.uuid4().hex[:6]}", name="Other", is_active=True
    )
    db_session.add(other)
    await db_session.flush()
    foreign = await _run_with_steps(db_session, other.id, steps=_ledger(2))
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)

    resp = await app_client.get(
        f"/api/v1/admin/agent-runs/{foreign.id}/steps", headers=_headers(support)
    )

    assert resp.status_code == 404


async def test_a_plain_user_may_not_read_a_ledger(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    run = await _run_with_steps(db_session, default_tenant.id, steps=_ledger(1))
    plain = await _user(db_session, default_tenant.id, UserRole.USER)

    resp = await app_client.get(
        f"/api/v1/admin/agent-runs/{run.id}/steps", headers=_headers(plain)
    )
    anonymous = await app_client.get(f"/api/v1/admin/agent-runs/{run.id}/steps")

    assert resp.status_code == 403
    assert anonymous.status_code == 401


async def test_a_negative_after_seq_is_refused(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    run = await _run_with_steps(db_session, default_tenant.id, steps=_ledger(1))
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)

    resp = await app_client.get(
        f"/api/v1/admin/agent-runs/{run.id}/steps?after_seq=-1",
        headers=_headers(support),
    )

    assert resp.status_code == 422


async def test_the_run_shape_carries_the_reasoning_columns(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    """One request rebuilds a panel from cold; the ledger endpoint above is for keeping
    it up to date, not for the first paint."""
    run = await _run_with_steps(db_session, default_tenant.id, steps=_ledger(2))
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)

    body = (
        await app_client.get(
            f"/api/v1/admin/agent-runs/{run.id}", headers=_headers(support)
        )
    ).json()

    assert body["hypotheses"][0]["name"] == "a stalled consumer"
    assert body["plan"]["action_tool"] == "restart_consumer_group"
    assert body["verification"]["verdict"] == "verified"
    assert [v["attempt"] for v in body["verifications"]] == [1]
    assert [s["seq"] for s in body["steps"]] == [1, 2]
    assert body["steps_dropped"] == 0
    assert body["budget"]["tool_calls_max"] == 13


async def test_the_lag_reading_says_how_wide_its_window_is(
    app_client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,
    redis_stub: _RedisStub,
) -> None:
    """15 minutes, from the platform rather than from a chart's own assumption — the
    first take stitched the window together client-side and lost it on every reload."""
    from app.core.consumer_lag import LAG_SAMPLES_KEEP, LAG_SAMPLES_WINDOW_SECONDS

    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)
    redis_stub.store[lag_key(LIVE_REFRESHED_GROUP)] = "42"
    redis_stub.store[samples_key(LIVE_REFRESHED_GROUP)] = json.dumps(
        [
            {
                "lag": 42 - i,
                "measured_at": (_NOW - timedelta(seconds=60 * i)).isoformat(),
            }
            for i in range(LAG_SAMPLES_KEEP + 4)
        ]
    )

    body = (
        await app_client.get("/api/v1/admin/consumer-lag", headers=_headers(support))
    ).json()

    assert body["sample_window_seconds"] == LAG_SAMPLES_WINDOW_SECONDS == 900
    assert body["sample_interval_seconds"] == 60
    live = next(
        g for g in body["groups"] if g["consumer_group"] == LIVE_REFRESHED_GROUP
    )
    # The reader bounds what it hands back at the window's own cap, whatever is stored.
    assert len(live["recent_samples"]) == LAG_SAMPLES_KEEP
    assert [s["lag"] for s in live["recent_samples"]] == sorted(
        (s["lag"] for s in live["recent_samples"]), reverse=True
    ), "newest first"


async def test_the_listing_omits_the_ledger_and_the_single_read_carries_it(
    app_client: AsyncClient, db_session: AsyncSession, default_tenant
) -> None:
    """A page of 100 runs with 200 steps each is megabytes an operator never asked for, so
    the ledger is ABSENT from the listing rather than emptied — an empty list would read
    as "this run made no calls". Everything else about the run is on both shapes."""
    run = await _run_with_steps(db_session, default_tenant.id, steps=_ledger(3))
    support = await _user(db_session, default_tenant.id, UserRole.SUPPORT)

    listed = (
        await app_client.get("/api/v1/admin/agent-runs", headers=_headers(support))
    ).json()["items"][0]
    single = (
        await app_client.get(
            f"/api/v1/admin/agent-runs/{run.id}", headers=_headers(support)
        )
    ).json()

    assert "steps" not in listed
    assert [s["seq"] for s in single["steps"]] == [1, 2, 3]
    # The small columns are on both, so a run selector needs one request.
    for field in (
        "hypotheses",
        "plan",
        "verification",
        "verifications",
        "steps_dropped",
        "budget",
        "phase_history",
        "active",
    ):
        assert field in listed, field
