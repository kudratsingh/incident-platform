"""End-to-end tests for Wave 2 PR C — the 9 read tools.

Same pattern as `test_mcp_standalone.py`: build the standalone MCP
app, mint a scoped SA token, POST JSON-RPC, assert shape.

One test class per tool grouping:
  - list_dlq_messages
  - get_trace / search_traces
  - get_dag_state
  - get_redis_health / get_postgres_health
  - get_deploy_history
  - get_incident / list_incidents
"""

from __future__ import annotations

import json
import pathlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest_asyncio
from app.core.scopes import Scope
from app.dependencies import get_db, get_redis
from app.mcp import protocol
from app.mcp.standalone import create_mcp_app
from app.models.alert import Alert
from app.models.enums import JobStatus, JobType
from app.models.job import Job
from app.models.job_dependency import JobDependency
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.services.service_account import ServiceAccountService
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


class _RedisStub:
    """Enough of the async Redis surface for the tools we exercise:
    `get`, `set`, `mget`, `ttl`, `ping`, `info`.

    `mget`/`ttl` back the DAG pause reads in `get_dag_state`. They have
    to be real here: `app.utils.dag_pause` fails open on any exception,
    so a missing stub method would silently report "not paused" and the
    pause assertions below would pass without testing anything."""

    def __init__(self, info: dict[str, Any] | None = None) -> None:
        self._store: dict[str, bytes | str] = {}
        self._ttls: dict[str, int] = {}
        self._info = info or {
            "connected_clients": 3,
            "used_memory": 12_500_000,
            "used_memory_human": "12M",
            "keyspace_hits": 100,
            "keyspace_misses": 7,
        }

    async def get(self, key: str) -> bytes | str | None:
        return self._store.get(key)

    async def set(self, key: str, value: bytes | str, ex: int | None = None) -> bool:
        self._store[key] = value
        if ex is not None:
            self._ttls[key] = ex
        return True

    async def mget(self, keys: list[str]) -> list[bytes | str | None]:
        return [self._store.get(k) for k in keys]

    async def ttl(self, key: str) -> int:
        # redis-py convention: -2 missing, -1 present with no expiry.
        if key not in self._store:
            return -2
        return self._ttls.get(key, -1)

    async def ping(self) -> bool:
        return True

    async def info(self) -> dict[str, Any]:
        return dict(self._info)


@pytest_asyncio.fixture
async def mcp_client(  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
):
    app = create_mcp_app()
    redis_stub = _RedisStub()

    async def _override_db():
        yield db_session

    async def _override_redis():
        yield redis_stub

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        yield ac, redis_stub


async def _token(
    db_session: AsyncSession, tenant_id: uuid.UUID, scopes: list[str]
) -> str:
    svc = ServiceAccountService(
        ServiceAccountRepository(db_session),
        ServiceAccountTokenRepository(db_session),
        AuditRepository(db_session),
    )
    sa = await svc.create_service_account(
        tenant_id=tenant_id,
        name=f"probe-{uuid.uuid4().hex[:8]}",
        scopes=scopes,
        created_by_user_id=None,
    )
    _, plaintext = await svc.mint_token(
        service_account=sa,
        scopes=None,
        ttl=None,
        minted_by_user_id=None,
    )
    return plaintext


def _rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": params or {}}


async def _call(
    ac: AsyncClient, token: str, tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    resp = await ac.post(
        "/mcp",
        json=_rpc("tools/call", {"name": tool_name, "arguments": arguments}),
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp.json()


def _content(body: dict[str, Any]) -> dict[str, Any]:
    return json.loads(body["result"]["content"][0]["text"])


# ---------------------------------------------------------------------------
# list_dlq_messages
# ---------------------------------------------------------------------------


async def test_list_dlq_messages_returns_dead_letter_jobs(
    mcp_client, db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    ac, _ = mcp_client
    db_session.add(
        Job(
            tenant_id=default_tenant.id,
            user_id=test_user.id,
            type=JobType.CSV_UPLOAD.value,
            status=JobStatus.DEAD_LETTER.value,
            error_message="csv parse failed",
            retry_count=3,
            trace_id="trace-dlq-1",
        )
    )
    db_session.add(
        Job(
            tenant_id=default_tenant.id,
            user_id=test_user.id,
            type=JobType.BULK_API_SYNC.value,
            status=JobStatus.COMPLETED.value,
        )
    )
    await db_session.flush()

    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    body = await _call(ac, token, "list_dlq_messages", {})
    assert "result" in body, body
    payload = _content(body)
    assert payload["total"] == 1
    assert payload["items"][0]["type"] == JobType.CSV_UPLOAD.value
    assert payload["items"][0]["error_message"] == "csv parse failed"


async def test_list_dlq_messages_wrong_scope_forbidden(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    ac, _ = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    body = await _call(ac, token, "list_dlq_messages", {})
    assert body["error"]["code"] == protocol.MCP_FORBIDDEN


# ---------------------------------------------------------------------------
# get_trace / search_traces
# ---------------------------------------------------------------------------


async def test_get_trace_returns_matching_job(
    mcp_client, db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    ac, _ = mcp_client
    db_session.add(
        Job(
            tenant_id=default_tenant.id,
            user_id=test_user.id,
            type=JobType.CSV_UPLOAD.value,
            status=JobStatus.COMPLETED.value,
            trace_id="trace-abc-123",
        )
    )
    await db_session.flush()

    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    payload = _content(
        await _call(
            ac, token, "get_trace", {"trace_id": "trace-abc-123"}
        )
    )
    assert payload["trace_id"] == "trace-abc-123"
    assert len(payload["jobs"]) == 1
    assert payload["jobs"][0]["status"] == JobStatus.COMPLETED.value


async def test_search_traces_filters_by_status(
    mcp_client, db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    ac, _ = mcp_client
    for i in range(3):
        db_session.add(
            Job(
                tenant_id=default_tenant.id,
                user_id=test_user.id,
                type=JobType.CSV_UPLOAD.value,
                status=JobStatus.FAILED.value,
                trace_id=f"tr-failed-{i}",
            )
        )
    db_session.add(
        Job(
            tenant_id=default_tenant.id,
            user_id=test_user.id,
            type=JobType.CSV_UPLOAD.value,
            status=JobStatus.COMPLETED.value,
            trace_id="tr-ok-1",
        )
    )
    await db_session.flush()

    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    payload = _content(
        await _call(
            ac,
            token,
            "search_traces",
            {"status": JobStatus.FAILED.value, "limit": 10},
        )
    )
    trace_ids = {m["trace_id"] for m in payload["matches"]}
    assert trace_ids == {"tr-failed-0", "tr-failed-1", "tr-failed-2"}


# ---------------------------------------------------------------------------
# get_dag_state
# ---------------------------------------------------------------------------


async def test_get_dag_state_returns_parents_and_children(
    mcp_client, db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    ac, _ = mcp_client
    parent = Job(
        tenant_id=default_tenant.id,
        user_id=test_user.id,
        type=JobType.CSV_UPLOAD.value,
        status=JobStatus.COMPLETED.value,
    )
    seed = Job(
        tenant_id=default_tenant.id,
        user_id=test_user.id,
        type=JobType.REPORT_GEN.value,
        status=JobStatus.RUNNING.value,
    )
    child = Job(
        tenant_id=default_tenant.id,
        user_id=test_user.id,
        type=JobType.BULK_API_SYNC.value,
        status=JobStatus.WAITING.value,
    )
    for j in (parent, seed, child):
        db_session.add(j)
    await db_session.flush()

    db_session.add(JobDependency(job_id=seed.id, depends_on_job_id=parent.id))
    db_session.add(JobDependency(job_id=child.id, depends_on_job_id=seed.id))
    await db_session.flush()

    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    payload = _content(
        await _call(
            ac, token, "get_dag_state", {"job_id": str(seed.id)}
        )
    )
    ids = {n["id"] for n in payload["nodes"]}
    assert ids == {str(parent.id), str(seed.id), str(child.id)}
    # Two edges: seed -> parent (upward) and child -> seed (downward).
    assert len(payload["edges"]) == 2


async def test_get_dag_state_reports_unpaused_by_default(
    mcp_client, db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The pause fields are part of the output contract even when no
    pause exists — the agent branches on them unconditionally."""
    ac, _ = mcp_client
    seed = Job(
        tenant_id=default_tenant.id,
        user_id=test_user.id,
        type=JobType.REPORT_GEN.value,
        status=JobStatus.WAITING.value,
    )
    db_session.add(seed)
    await db_session.flush()

    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    payload = _content(
        await _call(ac, token, "get_dag_state", {"job_id": str(seed.id)})
    )
    assert payload["paused"] is False
    assert payload["paused_by"] is None
    assert payload["paused_expires_in_seconds"] is None


async def test_get_dag_state_reports_pause_and_expiry(
    mcp_client, db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """This is the verification surface runaway_saga needs: after
    pause_dag, the seed reads back paused with a countdown."""
    ac, redis_stub = mcp_client
    seed = Job(
        tenant_id=default_tenant.id,
        user_id=test_user.id,
        type=JobType.REPORT_GEN.value,
        status=JobStatus.WAITING.value,
    )
    db_session.add(seed)
    await db_session.flush()

    await redis_stub.set(f"dag:paused:{seed.id}", "paused", ex=600)

    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    payload = _content(
        await _call(ac, token, "get_dag_state", {"job_id": str(seed.id)})
    )
    assert payload["paused"] is True
    assert payload["paused_expires_in_seconds"] == 600
    assert payload["paused_by"] == str(seed.id)


async def test_get_dag_state_attributes_pause_to_ancestor(
    mcp_client, db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """A child paused via its root reports paused=False (no flag of its
    own) but paused_by=<root> — the distinction that tells the agent
    where to aim a resume."""
    ac, redis_stub = mcp_client
    root = Job(
        tenant_id=default_tenant.id,
        user_id=test_user.id,
        type=JobType.CSV_UPLOAD.value,
        status=JobStatus.COMPLETED.value,
    )
    child = Job(
        tenant_id=default_tenant.id,
        user_id=test_user.id,
        type=JobType.BULK_API_SYNC.value,
        status=JobStatus.WAITING.value,
    )
    db_session.add_all([root, child])
    await db_session.flush()
    db_session.add(JobDependency(job_id=child.id, depends_on_job_id=root.id))
    await db_session.flush()

    await redis_stub.set(f"dag:paused:{root.id}", "paused", ex=300)

    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    payload = _content(
        await _call(ac, token, "get_dag_state", {"job_id": str(child.id)})
    )
    assert payload["paused"] is False
    assert payload["paused_by"] == str(root.id)


async def test_get_dag_state_unknown_job_404(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    ac, _ = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    body = await _call(
        ac,
        token,
        "get_dag_state",
        {"job_id": str(uuid.uuid4())},
    )
    assert body["error"]["code"] == protocol.MCP_TOOL_ERROR
    assert body["error"]["data"]["error_code"] == "not_found"


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


async def test_get_redis_health_returns_stats(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    ac, _ = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    payload = _content(await _call(ac, token, "get_redis_health", {}))
    assert payload["ok"] is True
    assert payload["connected_clients"] == 3
    assert payload["used_memory_bytes"] == 12_500_000


async def test_get_postgres_health_reports_dialect(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    ac, _ = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    payload = _content(await _call(ac, token, "get_postgres_health", {}))
    assert payload["ok"] is True
    assert payload["dialect"] == "sqlite"
    # Non-Postgres branch skips the pg_stat_activity read.
    assert payload["active_connections"] is None


# ---------------------------------------------------------------------------
# get_deploy_history
# ---------------------------------------------------------------------------


async def test_get_deploy_history_env_fallback_when_table_empty(
    mcp_client, db_session: AsyncSession, default_tenant, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    ac, _ = mcp_client
    monkeypatch.setenv("APP_VERSION", "v0.4.0-test")
    monkeypatch.setenv("APP_REVISION", "abc1234")

    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    payload = _content(await _call(ac, token, "get_deploy_history", {}))
    assert payload["source"] == "env"
    assert payload["total"] == 1
    entry = payload["entries"][0]
    assert entry["version"] == "v0.4.0-test"
    assert entry["revision"] == "abc1234"


async def test_get_deploy_history_reports_unknown_when_env_missing(
    mcp_client, db_session: AsyncSession, default_tenant, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    ac, _ = mcp_client
    for k in ("APP_VERSION", "APP_REVISION", "BACKEND_IMAGE_TAG"):
        monkeypatch.delenv(k, raising=False)
    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    payload = _content(await _call(ac, token, "get_deploy_history", {}))
    assert payload["source"] == "env"
    assert payload["entries"][0]["version"] == "unknown"


async def test_get_deploy_history_prefers_deploy_markers_table(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """When rows exist in `deploy_markers`, the tool reads from there
    instead of falling back to env — the source flips to
    `deploy_markers` and rows come back in newest-first order."""
    from app.models.deploy_marker import DeployMarker

    now = datetime.now(UTC)
    db_session.add(
        DeployMarker(
            version="v0.4.2",
            revision="c9f4d02",
            image_tag="v0.4.2",
            environment="prod",
            deployed_at=now - timedelta(hours=6),
            notes="correlated with billing failures",
        )
    )
    db_session.add(
        DeployMarker(
            version="v0.4.1",
            environment="prod",
            deployed_at=now - timedelta(hours=18),
        )
    )
    await db_session.flush()

    ac, _ = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    payload = _content(await _call(ac, token, "get_deploy_history", {}))
    assert payload["source"] == "deploy_markers"
    assert payload["total"] == 2
    # Newest first
    assert payload["entries"][0]["version"] == "v0.4.2"
    assert (
        payload["entries"][0]["notes"] == "correlated with billing failures"
    )


async def test_get_deploy_history_environment_filter(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    from app.models.deploy_marker import DeployMarker

    now = datetime.now(UTC)
    db_session.add(
        DeployMarker(version="v0.4.0", environment="prod", deployed_at=now)
    )
    db_session.add(
        DeployMarker(
            version="v0.4.0-rc1", environment="staging", deployed_at=now
        )
    )
    await db_session.flush()

    ac, _ = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    payload = _content(
        await _call(ac, token, "get_deploy_history", {"environment": "prod"})
    )
    assert payload["source"] == "deploy_markers"
    assert payload["total"] == 1
    assert payload["entries"][0]["environment"] == "prod"


async def test_get_deploy_history_falls_back_to_env_on_db_error(
    mcp_client, db_session: AsyncSession, default_tenant, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    """The P0 fix — if the deploy_markers query blows up (missing table
    during a staged rollout, transient connection issue, etc.) the tool
    must NOT surface HTTP 500. It falls back to the env-based synthetic
    entry and stamps `source: env` with a note explaining the fallback."""
    from sqlalchemy.exc import ProgrammingError

    monkeypatch.setenv("APP_VERSION", "v0.9.9-fallback")

    async def _boom(*args, **kwargs):
        raise ProgrammingError(
            "SELECT deploy_markers", {}, Exception("relation does not exist")
        )

    from app.repositories.deploy_marker import DeployMarkerRepository

    monkeypatch.setattr(DeployMarkerRepository, "list_recent", _boom)

    ac, _ = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    payload = _content(await _call(ac, token, "get_deploy_history", {}))
    assert payload["source"] == "env"
    assert payload["total"] == 1
    entry = payload["entries"][0]
    assert entry["version"] == "v0.9.9-fallback"
    assert "deploy_markers query failed" in entry["notes"]


async def test_seed_pins_manifest_shape() -> None:
    """The pin manifest that the seed script writes to
    `/app/eval-fixtures-pins.json` — scenarios pin against these UUIDs,
    so the shape needs to be stable."""
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
    from scripts.seed_eval_fixtures import collect_pins  # type: ignore

    manifest = collect_pins()
    assert "seed_user_id" in manifest
    assert set(manifest["consumer_groups"]) >= {
        "billing-consumer",
        "orders-consumer",
        "notifications-consumer",
        "analytics-consumer",
        "payments-consumer",
        "shipping-consumer",
        "healthy-consumer",
    }
    assert isinstance(manifest["alerts"], list) and len(manifest["alerts"]) >= 3
    assert any(a["state"] == "active" for a in manifest["alerts"])
    assert any(a["state"] == "resolved" for a in manifest["alerts"])
    assert "dag" in manifest and "seed_job_id" in manifest["dag"]


# ---------------------------------------------------------------------------
# get_consumer_lag — dynamic key derivation
# ---------------------------------------------------------------------------


async def test_get_consumer_lag_arbitrary_group_reads_correct_key(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """Any group name derives its Redis key — no hardcoded map."""
    ac, redis_stub = mcp_client
    redis_stub._store["kafka:consumer_lag:billing-consumer"] = "15000"

    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    payload = _content(
        await _call(
            ac,
            token,
            "get_consumer_lag",
            {"consumer_group": "billing-consumer"},
        )
    )
    assert payload["consumer_group"] == "billing-consumer"
    assert payload["lag"] == 15000
    assert payload["cache_key"] == "kafka:consumer_lag:billing-consumer"


async def test_get_consumer_lag_unknown_group_returns_null_not_error(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """Unknown groups return lag: null — never an error. Defensive per
    the incident-commander live-eval spec (one scenario probes an
    unknown group intentionally)."""
    ac, _ = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    payload = _content(
        await _call(
            ac,
            token,
            "get_consumer_lag",
            {"consumer_group": "never-existed"},
        )
    )
    assert payload["consumer_group"] == "never-existed"
    assert payload["lag"] is None
    assert payload["cache_key"] == "kafka:consumer_lag:never-existed"


# ---------------------------------------------------------------------------
# list_incidents / get_incident
# ---------------------------------------------------------------------------


async def test_list_incidents_defaults_to_unresolved(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    ac, _ = mcp_client
    db_session.add(
        Alert(
            tenant_id=default_tenant.id,
            severity="warning",
            source="slo",
            title="Active",
        )
    )
    db_session.add(
        Alert(
            tenant_id=default_tenant.id,
            severity="info",
            source="dlq",
            title="Resolved",
            resolved_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    payload = _content(await _call(ac, token, "list_incidents", {}))
    titles = {i["title"] for i in payload["incidents"]}
    assert titles == {"Active"}


async def test_list_incidents_include_resolved_returns_both(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    ac, _ = mcp_client
    db_session.add(
        Alert(
            tenant_id=default_tenant.id,
            severity="info",
            source="s",
            title="A",
        )
    )
    db_session.add(
        Alert(
            tenant_id=default_tenant.id,
            severity="warning",
            source="s",
            title="B",
            resolved_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    payload = _content(
        await _call(ac, token, "list_incidents", {"include_resolved": True})
    )
    titles = {i["title"] for i in payload["incidents"]}
    assert titles == {"A", "B"}


async def test_get_incident_returns_row_by_id(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    ac, _ = mcp_client
    alert = Alert(
        tenant_id=default_tenant.id,
        severity="critical",
        source="chaos",
        title="Chaos test",
        extra_data={"reason": "manual"},
    )
    db_session.add(alert)
    await db_session.flush()

    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    payload = _content(
        await _call(ac, token, "get_incident", {"id": str(alert.id)})
    )
    assert payload["title"] == "Chaos test"
    assert payload["severity"] == "critical"
    assert payload["extra_data"] == {"reason": "manual"}


async def test_get_incident_unknown_id_returns_not_found(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    ac, _ = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    body = await _call(
        ac, token, "get_incident", {"id": str(uuid.uuid4())}
    )
    assert body["error"]["code"] == protocol.MCP_TOOL_ERROR
    assert body["error"]["data"]["error_code"] == "not_found"
