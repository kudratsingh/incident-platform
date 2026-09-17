"""No read tool's RESPONSE says `chaos` to a principal without `chaos:invoke`.

WO-R3-187 (owner decision O-4), the regression half. `test_lab_invisibility`
already screens every non-chaos tool's `tools/list` surface — description,
`inputSchema`, `outputSchema` — for lab vocabulary. Nothing screened what
comes back from a `tools/call` on a world the lab has actually seeded, which
is where the leak this work order closes lived: `list_audit_events` returned
`chaos.tool_invoked` rows naming the hook and its arguments.

Scope, stated exactly, because both halves are deliberate:

  - **Responses, every read tool, parameterised off the registry.** A tool
    added later is picked up automatically: `_ARGUMENTS` must cover the
    registry or the sweep fails naming the tool it cannot call. That failure
    is the point — the author of the next read tool decides what a sensible
    call looks like, and cannot skip the screen by forgetting it exists.
  - **Descriptions are NOT in scope.** Chaos tools' descriptions carry a
    `[chaos: <blast_radius>]` prefix and `tools/list` is not principal-scoped,
    so a read-scoped token can enumerate them. That is recorded, deferred and
    load-bearing elsewhere: ADR 0016 defers principal-scoped `tools/list`,
    and the commander identifies chaos hooks in its contract snapshot by that
    exact prefix, so masking them would silently empty every scenario's
    `chaos_setup` validation. Divergence report row G4.

The residual response-side leaks this sweep does not yet cover are named in
`_KNOWN_RESIDUAL_LEAKS` below, each with a tripwire test that fails when its
channel changes — so the list cannot quietly become stale, and closing one
forces this file to grow instead of being forgotten.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import app.mcp.tools  # noqa: F401  — import for @tool registration side effects
import pytest
import pytest_asyncio
from app.config import get_settings
from app.core.outbox_heartbeat import RELAY_TICK_KEY
from app.core.scopes import Scope
from app.dependencies import get_db, get_redis
from app.mcp.registry import ToolDefinition, list_tools
from app.mcp.standalone import create_mcp_app
from app.mcp.tools.chaos.poison_message import _dlq_error_for_topic
from app.models.alert import Alert
from app.models.audit import PRINCIPAL_TYPE_SERVICE_ACCOUNT, AuditLog
from app.models.enums import JobStatus, JobType
from app.models.job import Job
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.services.operator_audit import (
    CHAOS_TOOL_DENIED_ACTION,
    CHAOS_TOOL_INVOKED_ACTION,
)
from app.services.service_account import ServiceAccountService
from app.workers.control_loop_pause import ControlLoopName, pause_key_for
from app.workers.kafka_consumer import kill_key_for
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

# The word, and the case-insensitive test for it. Deliberately the bare
# substring rather than `test_lab_invisibility`'s inflection-aware pattern:
# this screen reads DATA, not prose, so `chaos:bad_deploy`,
# `chaos-owner+…@chaos.local` and `chaos:sat:{run}` all have to trip it, and
# none of them is a word with an inflection.
_BANNED = "chaos"

# The scopes the agent token carries after the split this work order lands
# (`scripts/seed_incident_commander.py`): read the platform, act on it, never
# fire the lab.
AGENT_SCOPES = [
    Scope.TELEMETRY_READ.value,
    Scope.INCIDENTS_READ.value,
    Scope.ACTIONS_EXECUTE.value,
]

_READ_SCOPES = {Scope.TELEMETRY_READ, Scope.INCIDENTS_READ}

# Fixed ids so the argument table can reference the seeded world without a
# fixture handshake.
_TRACE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_DLQ_JOB_ID = uuid.UUID("dddddddd-0000-4000-8000-000000000001")
_ALERT_ID = uuid.UUID("dddddddd-0000-4000-8000-000000000002")

#: Response-side channels that still carry the word, why, and what has to
#: happen before they can join the sweep. Each has a tripwire test below.
_KNOWN_RESIDUAL_LEAKS = {
    "bad_deploy alert": (
        "`bad_deploy` fires an alert with source `chaos:bad_deploy` and "
        "title `Simulated bad deploy`, both returned verbatim by "
        "`list_active_alerts` / `list_incidents` / `get_incident`. Renaming "
        "the source is blocked on owner decision O-8: "
        "`reset_eval_state.py::_resolve_chaos_alerts` matches "
        "`source LIKE 'chaos:%'`, so a renamed alert survives every reset "
        "and becomes a permanent distractor (WO-R2-131, reintroduced). The "
        "rename and the reset predicate have to move in one PR."
    ),
    "chaos-owner job owner": (
        "`create_bad_data_job` owns its job with a user whose email is "
        "`chaos-owner+{tenant}@chaos.local`. No read tool emits an owner "
        "email today — every job-bearing response carries `user_id` only — "
        "so this is latent rather than live. A future read tool that joins "
        "the owner in would open it."
    ),
}


class _RedisStub:
    """Enough Redis for the read surface, seeded the way a chaos run leaves it.

    The `chaos:*` keys are here on purpose: `saturate_redis`, `bad_deploy` and
    `pause_control_loop` leave exactly these behind, and a read tool that
    enumerated keys — none does today — would hand the agent the lab.
    `get_cache_key_info` cannot name them: `chaos:` is not in its
    readable-prefix allowlist, and the refusal it returns for one only ever
    quotes the key the caller supplied.

    `chaos:pause:outbox_relay` is what makes the sweep below the assertion
    WO-R3-200 needs: the pause hook's whole effect is that key, and the world
    a `jobs_not_progressing` scenario hands the agent has it set while the
    agent reads every probe here.

    `chaos:pause:resume_unblocked_waiting` plus `chaos:kill:dependency-resolver`
    are Family C's world (WO-R3-213), and they are here because that world is the
    first that arms **two** keys from two different mechanisms at once. A scenario
    whose correct answer is to escalate is the one where naming the lab does most
    damage: there is nothing to fix, so a hint that something was done *to* the
    platform is the only lead in the world. Both keys are set while every read
    tool below is called.

    `outbox:relay:last_tick` is the other half of that world (WO-R3-201). It is
    the relay's own record of its last pass, and in a paused-relay world it is
    stale — several minutes old — because the pass that would refresh it is the
    pass that is not running. `get_outbox_status` therefore reads a real,
    non-null heartbeat age here rather than the "unknown" branch, which is what
    makes its response worth screening: the tool is being called against the
    very world it exists to describe, and it still must not name the lab.
    """

    def __init__(self) -> None:
        self._store: dict[str, str] = {
            "kafka:consumer_lag:worker-dispatcher": "0",
            f"cache:job:{{tenant}}:{_DLQ_JOB_ID}": "{}",
            "chaos:bad_deploy": "1",
            "chaos:sat:run-1": "x" * 32,
            "chaos:pause:outbox_relay": "paused",
            pause_key_for(ControlLoopName.RESUME_UNBLOCKED_WAITING): "paused",
            kill_key_for(
                get_settings().kafka_consumer_group_dependency
            ): "killed",
            RELAY_TICK_KEY: (
                datetime.now(UTC) - timedelta(seconds=390)
            ).isoformat(),
        }

    def seed_tenant_cache_key(self, tenant_id: uuid.UUID) -> str:
        key = f"cache:job:{tenant_id}:{_DLQ_JOB_ID}"
        self._store[key] = "{}"
        return key

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def type(self, key: str) -> str:
        return "string" if key in self._store else "none"

    async def ttl(self, key: str) -> int:
        return -1 if key in self._store else -2

    async def strlen(self, key: str) -> int:
        return len(self._store.get(key, ""))

    async def ping(self) -> bool:
        return True

    async def info(self) -> dict[str, Any]:
        return {
            "connected_clients": 1,
            "used_memory": 1024,
            "used_memory_human": "1K",
            "keyspace_hits": 10,
            "keyspace_misses": 2,
        }


@pytest_asyncio.fixture
async def chaos_world(  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
    test_user: User,
):
    """A world a chaos run has been through, built at the data level.

    The hooks themselves are not fired here — that needs a broker, a real
    Redis and `CHAOS_ENABLED=true`, and bad_deploy would seed the
    residual alert leak listed above, which is not this PR's to close. What is
    reproduced is every row shape the hooks leave behind that a read tool
    can reach: the `chaos.` audit stream with its `tool_name` + `arguments`
    payload (the leak being closed), a declared DLQ fixture job, a seeded
    alert, and a trace shared between a chaos invocation and a real job.
    """
    tenant_id = default_tenant.id
    now = datetime.now(UTC)

    dlq_job = Job(
        id=_DLQ_JOB_ID,
        tenant_id=tenant_id,
        user_id=test_user.id,
        type=JobType.CSV_UPLOAD.value,
        status=JobStatus.DEAD_LETTER.value,
        payload={
            "seeded_fixture": True,
            "chaos_fixture": "poison_message",
            "fixture_name": "poison-message",
            "topic": "job.submitted",
        },
        error_message=_dlq_error_for_topic("job.submitted", "human_required"),
        retry_count=3,
        remediation_hint="human_required",
        trace_id=_TRACE_ID,
        # `list_dlq_messages` emits this as `dead_lettered_at`.
        completed_at=now - timedelta(minutes=5),
    )
    live_job = Job(
        tenant_id=tenant_id,
        user_id=test_user.id,
        type=JobType.REPORT_GEN.value,
        status=JobStatus.COMPLETED.value,
        payload={},
        retry_count=0,
        trace_id=_TRACE_ID,
    )
    alert = Alert(
        id=_ALERT_ID,
        tenant_id=tenant_id,
        severity="critical",
        source="slo:job_completion_rate",
        title="Job completion rate burning error budget",
        description="14.4x fast burn over the last hour.",
        extra_data={"burn_rate": 14.4},
    )
    audit_rows = [
        AuditLog(
            tenant_id=tenant_id,
            action="agent.tool_invoked",
            principal_type=PRINCIPAL_TYPE_SERVICE_ACCOUNT,
            principal_id=uuid.uuid4(),
            resource_type="mcp_tool",
            resource_id="list_dlq_messages",
            request_id=_TRACE_ID,
            extra_data={"tool_name": "list_dlq_messages", "outcome": "success"},
        ),
        AuditLog(
            tenant_id=tenant_id,
            action=CHAOS_TOOL_INVOKED_ACTION,
            principal_type=PRINCIPAL_TYPE_SERVICE_ACCOUNT,
            principal_id=uuid.uuid4(),
            resource_type="mcp_tool",
            resource_id="seed_dlq_messages",
            request_id=_TRACE_ID,
            extra_data={
                "tool_name": "seed_dlq_messages",
                "arguments": {"count": 4, "hint": "human_required"},
                "scope_used": Scope.CHAOS_INVOKE.value,
                "outcome": "success",
            },
        ),
        AuditLog(
            tenant_id=tenant_id,
            action=CHAOS_TOOL_DENIED_ACTION,
            principal_type=PRINCIPAL_TYPE_SERVICE_ACCOUNT,
            principal_id=uuid.uuid4(),
            resource_type="mcp_tool",
            resource_id="kill_consumer",
            request_id=_TRACE_ID,
            extra_data={
                "tool_name": "kill_consumer",
                "arguments": {"consumer_group": "worker-dispatcher"},
                "denied_by": "scope_check",
                "outcome": "unauthorized",
            },
        ),
    ]

    for row in (dlq_job, live_job, alert, *audit_rows):
        db_session.add(row)
    await db_session.flush()
    return tenant_id


@pytest_asyncio.fixture
async def agent_client(  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    chaos_world: uuid.UUID,
):
    """The MCP app, a `_RedisStub`, and a token with the agent's scopes."""
    redis = _RedisStub()
    cache_key = redis.seed_tenant_cache_key(chaos_world)
    app = create_mcp_app()

    async def _override_db():
        yield db_session

    async def _override_redis():
        yield redis

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis

    svc = ServiceAccountService(
        ServiceAccountRepository(db_session),
        ServiceAccountTokenRepository(db_session),
        AuditRepository(db_session),
    )
    sa = await svc.create_service_account(
        tenant_id=chaos_world,
        name=f"agent-{uuid.uuid4().hex[:8]}",
        scopes=list(AGENT_SCOPES),
        created_by_user_id=None,
    )
    _, token = await svc.mint_token(
        service_account=sa, scopes=None, ttl=None, minted_by_user_id=None
    )

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        yield ac, token, cache_key


def _read_tools() -> list[ToolDefinition]:
    """Every registered tool a read-scoped principal may call.

    Keyed on the scope, not on a name list: `telemetry:read` and
    `incidents:read` are the two read scopes in ADR 0007's taxonomy, so this
    is the whole surface the agent's investigation runs on. Action tools
    (`actions:execute`) are excluded because calling them mutates; chaos
    tools are excluded because they are the lab and may name it.
    """
    return [t for t in list_tools() if t.required_scope in _READ_SCOPES]


#: One sensible call per read tool. `{cache_key}` is substituted with the
#: tenant-scoped Redis key the fixture seeded.
_ARGUMENTS: dict[str, dict[str, Any]] = {
    "get_cache_key_info": {"key": "{cache_key}"},
    "get_consumer_lag": {"consumer_group": "worker-dispatcher"},
    "get_dag_state": {"job_id": str(_DLQ_JOB_ID)},
    "get_deploy_history": {},
    "get_incident": {"id": str(_ALERT_ID)},
    "get_outbox_status": {},
    "get_postgres_health": {},
    "get_redis_health": {},
    "get_trace": {"trace_id": _TRACE_ID},
    "list_active_alerts": {},
    "list_audit_events": {},
    "list_dlq_messages": {},
    "list_incidents": {"include_resolved": True},
    "search_traces": {},
}


def test_every_read_tool_has_a_call_in_the_argument_table() -> None:
    """The registry is the authority; this table has to keep up with it.

    A new read tool fails here by name, with the reason: the sweep below
    parameterises off `_read_tools()`, and a tool nobody supplied arguments
    for would otherwise be silently unscreened.
    """
    missing = sorted(t.name for t in _read_tools() if t.name not in _ARGUMENTS)
    assert missing == [], (
        f"read tools with no entry in _ARGUMENTS: {missing}. Add a sensible "
        "call for each so the response screen below covers it."
    )
    assert len(_read_tools()) >= 14, "the read surface shrank — check why"


@pytest.mark.parametrize(
    "tool_name", sorted(t.name for t in _read_tools())
)
async def test_read_tool_response_never_names_the_lab(
    agent_client: tuple[AsyncClient, str, str],
    tool_name: str,
) -> None:
    """THE sweep. Call the tool as the agent; screen the whole envelope.

    The screen is on the serialized JSON-RPC response, not on a parsed
    field: an error message, a validation detail and a nested `extra_data`
    blob are all things the agent reads, and the first version of this leak
    lived inside `extra_data`.
    """
    ac, token, cache_key = agent_client
    arguments = {
        k: (cache_key if v == "{cache_key}" else v)
        for k, v in _ARGUMENTS[tool_name].items()
    }

    resp = await ac.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()

    # A tool that errors proves nothing about the world it was supposed to
    # read, so the call has to have actually run. Every read tool on this
    # harness can: the health probes report their own failures as data
    # rather than raising, and the rest read Postgres or the Redis stub.
    assert "error" not in body, (
        f"{tool_name} did not run — fix the call in _ARGUMENTS rather than "
        f"letting an error response pass the screen: {body.get('error')}"
    )

    serialized = json.dumps(body)
    assert _BANNED not in serialized.lower(), (
        f"{tool_name} named the lab in its response: {serialized}"
    )


async def test_the_sweep_would_catch_the_leak_it_closed(
    agent_client: tuple[AsyncClient, str, str],
    db_session: AsyncSession,
    chaos_world: uuid.UUID,
) -> None:
    """The screen is load-bearing, so prove it fires.

    Same seeded world, same call, one difference: the token also holds
    `chaos:invoke`, which is what makes the chaos rows visible. The response
    then contains the word — i.e. the sweep above is passing because the
    filter works, not because the world is empty or the screen is inert.
    """
    ac, _agent_token, _cache_key = agent_client
    svc = ServiceAccountService(
        ServiceAccountRepository(db_session),
        ServiceAccountTokenRepository(db_session),
        AuditRepository(db_session),
    )
    sa = await svc.create_service_account(
        tenant_id=chaos_world,
        name=f"evaluator-{uuid.uuid4().hex[:8]}",
        scopes=[Scope.INCIDENTS_READ.value, Scope.CHAOS_INVOKE.value],
        created_by_user_id=None,
    )
    _, chaos_token = await svc.mint_token(
        service_account=sa, scopes=None, ttl=None, minted_by_user_id=None
    )

    resp = await ac.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tools/call",
            "params": {"name": "list_audit_events", "arguments": {}},
        },
        headers={"Authorization": f"Bearer {chaos_token}"},
    )
    assert _BANNED in json.dumps(resp.json()).lower(), (
        "the evaluator must still see the chaos stream — otherwise the "
        "sweep above proves nothing"
    )


# ---------------------------------------------------------------------------
# Tripwires on the residual leaks
# ---------------------------------------------------------------------------


def test_the_residual_leak_list_is_documented() -> None:
    """Each residual leak needs a reason, not just a name."""
    assert set(_KNOWN_RESIDUAL_LEAKS) == {
        "bad_deploy alert",
        "chaos-owner job owner",
    }
    for name, reason in _KNOWN_RESIDUAL_LEAKS.items():
        assert len(reason) > 80, f"{name} needs a reason a reader can act on"


def test_bad_deploy_alert_source_still_leaks_pending_o8() -> None:
    """Tripwire for owner decision O-8, not an endorsement.

    When the source is renamed (together with the reset predicate — see the
    entry in `_KNOWN_RESIDUAL_LEAKS`), this fails, and the fix is to seed a
    `bad_deploy` alert into `chaos_world` above and delete this test. Read
    off the module constants rather than the running registry because the
    chaos tools are unregistered under default settings.
    """
    from app.mcp.tools.chaos import bad_deploy

    assert bad_deploy.BAD_DEPLOY_KEY.startswith(f"{_BANNED}:"), (
        "bad_deploy's Redis key changed — re-read O-8 and update the "
        "residual-leak list"
    )
    source_default = bad_deploy.BadDeployInput.model_fields["label"].default
    assert _BANNED in str(source_default).lower(), (
        "bad_deploy's alert label no longer names the lab — if O-8 landed, "
        "add the alert to the seeded world and drop this tripwire"
    )
