"""The stuck chain's cause, read over the wire by the agent's own token (WO-R3-371, ADR 0041).

INC-008: `workflow_stuck_resolver_stall` is a killed `dependency-resolver` plus a paused
`resume_unblocked_waiting` sweep, and before this order neither was readable — the label the run
was graded on was a coin flip on identical evidence. This file is the agent's side: the two
readings exist, a read-scoped token is enough for both, and the lab stays invisible while they
are taken (ADR 0012) — the responses never name it and the `chaos.` rows that record the fault
stay withheld from the agent while the evaluator still reads them.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest_asyncio
from app.config import Settings
from app.core.consumer_lag import (
    DEPENDENCY_RESOLVER_GROUP,
    lag_key,
    record_lag_sample,
    record_last_poll,
)
from app.core.loop_heartbeat import loop_run_key
from app.core.outbox_heartbeat import RELAY_TICK_KEY
from app.core.scopes import Scope
from app.dependencies import get_db, get_redis
from app.mcp.registry import list_tools
from app.mcp.standalone import create_mcp_app
from app.models.audit import PRINCIPAL_TYPE_SERVICE_ACCOUNT, AuditLog
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.services.operator_audit import CHAOS_TOOL_INVOKED_ACTION
from app.services.service_account import ServiceAccountService
from app.workers.control_loop_pause import ControlLoopName, pause_key_for
from app.workers.kafka_consumer import kill_key_for
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

AGENT_SCOPES = [
    Scope.TELEMETRY_READ.value,
    Scope.INCIDENTS_READ.value,
    Scope.ACTIONS_EXECUTE.value,
]
EVALUATOR_SCOPES = [
    Scope.TELEMETRY_READ.value,
    Scope.INCIDENTS_READ.value,
    Scope.CHAOS_INVOKE.value,
]

_PAUSE_TTL = 420
_LAB_ON = Settings(chaos_enabled=True, environment="test")


class _RedisStub:
    """get / set / ttl, with real TTL semantics — the pause reading is a TTL read."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.fail = False

    async def get(self, key: str) -> str | None:
        if self.fail:
            raise ConnectionError("redis unreachable")
        return self.store.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self.store[key] = str(value)
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def ttl(self, key: str) -> int:
        if self.fail:
            raise ConnectionError("redis unreachable")
        if key not in self.store:
            return -2
        return self.ttls.get(key, -1)


@pytest_asyncio.fixture
async def mcp(db_session: AsyncSession, default_tenant):  # type: ignore[no-untyped-def]
    app = create_mcp_app()
    redis = _RedisStub()

    async def _override_db():  # type: ignore[no-untyped-def]
        yield db_session

    async def _override_redis():  # type: ignore[no-untyped-def]
        yield redis

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        yield ac, redis


async def _token(db_session: AsyncSession, tenant_id: uuid.UUID, scopes: list[str]) -> str:
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
        service_account=sa, scopes=None, ttl=None, minted_by_user_id=None
    )
    return plaintext


async def _call(
    ac: AsyncClient, token: str, name: str, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    resp = await ac.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    body: dict[str, Any] = resp.json()
    return body


def _content(body: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in body, body.get("error")
    content: dict[str, Any] = json.loads(body["result"]["content"][0]["text"])
    return content


async def _resolver_world(redis: _RedisStub, *, polled_seconds_ago: float) -> None:
    """What one metrics pass leaves for the resolver: value, dated sample, poll time."""
    await redis.set(lag_key(DEPENDENCY_RESOLVER_GROUP), 0, ex=15)
    await record_lag_sample(redis, 0, group=DEPENDENCY_RESOLVER_GROUP)
    await record_last_poll(
        redis,
        datetime.now(UTC) - timedelta(seconds=polled_seconds_ago),
        group=DEPENDENCY_RESOLVER_GROUP,
    )


async def _stranded_world(redis: _RedisStub) -> None:
    """The `resolver_stall` world, as the lab leaves Redis: both keys, plus the loop stamps a
    running worker writes — every loop fresh except the paused sweep, which stopped stamping."""
    await _resolver_world(redis, polled_seconds_ago=140)
    await redis.set(kill_key_for(DEPENDENCY_RESOLVER_GROUP), "killed", ex=600)
    await redis.set(
        pause_key_for(ControlLoopName.RESUME_UNBLOCKED_WAITING), "paused", ex=_PAUSE_TTL
    )
    now = datetime.now(UTC)
    for loop in ControlLoopName:
        age = 140 if loop is ControlLoopName.RESUME_UNBLOCKED_WAITING else 1
        await redis.set(loop_run_key(loop), (now - timedelta(seconds=age)).isoformat())


# 1. `get_consumer_lag` knows the resolver


async def test_the_resolver_reads_live_with_a_fresh_poll(  # type: ignore[no-untyped-def]
    mcp, db_session: AsyncSession, default_tenant
) -> None:
    ac, redis = mcp
    await _resolver_world(redis, polled_seconds_ago=1)
    token = await _token(db_session, default_tenant.id, [Scope.TELEMETRY_READ.value])

    reading = _content(
        await _call(ac, token, "get_consumer_lag", {"consumer_group": "dependency-resolver"})
    )

    assert reading["source"] == "live", "was `unrecognized` before WO-R3-371 (INC-008)"
    assert reading["lag"] == 0
    assert reading["lag_known"] is True
    assert reading["age_seconds"] is not None
    assert len(reading["recent_samples"]) == 1
    assert reading["last_poll_age_seconds"] <= 3


async def test_a_killed_resolver_shows_in_the_poll_fields(  # type: ignore[no-untyped-def]
    mcp, db_session: AsyncSession, default_tenant
) -> None:
    ac, redis = mcp
    await _stranded_world(redis)
    token = await _token(db_session, default_tenant.id, AGENT_SCOPES)

    reading = _content(
        await _call(ac, token, "get_consumer_lag", {"consumer_group": "dependency-resolver"})
    )

    # The lag is flat and fresh — a quiet `job.completed` topic, so it cannot say "stopped"…
    assert (reading["lag"], reading["lag_known"], reading["source"]) == (0, True, "live")
    # …and the poll fields do.
    assert reading["last_poll_age_seconds"] >= 140
    assert reading["last_poll_age_seconds"] - reading["age_seconds"] >= 100
    assert "chaos" not in json.dumps(reading).lower()


async def test_the_description_lists_the_resolver_and_says_how_to_read_a_poll(  # type: ignore[no-untyped-def]
    mcp, db_session: AsyncSession, default_tenant
) -> None:
    ac, _ = mcp
    token = await _token(db_session, default_tenant.id, [Scope.TELEMETRY_READ.value])
    resp = await ac.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": "1", "method": "tools/list", "params": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    tools = {t["name"]: t for t in resp.json()["result"]["tools"]}
    lag = tools["get_consumer_lag"]

    first_line = lag["description"].splitlines()[0]
    assert "dependency-resolver" in first_line, "the known-groups list must name it"
    assert "last_poll_age_seconds" in lag["description"]
    group_field = lag["inputSchema"]["properties"]["consumer_group"]["description"]
    assert "dependency-resolver" in group_field
    assert {"last_poll_at", "last_poll_age_seconds"} <= set(
        lag["outputSchema"]["properties"]
    )


# 1b. WO-R3-373: the reading says whether the consumer is alive — `polling`, a verdict


async def test_over_the_wire_a_killed_resolver_reads_polling_false(  # type: ignore[no-untyped-def]
    mcp, db_session: AsyncSession, default_tenant
) -> None:
    """The INC-008 addendum's reading — lag 0, known, live, fresh — now carries its verdict, so
    nothing is left for the reader to threshold against a cadence nobody told it."""
    ac, redis = mcp
    await _stranded_world(redis)
    token = await _token(db_session, default_tenant.id, AGENT_SCOPES)

    reading = _content(
        await _call(ac, token, "get_consumer_lag", {"consumer_group": "dependency-resolver"})
    )

    assert (reading["lag"], reading["lag_known"], reading["source"]) == (0, True, "live")
    assert reading["polling"] is False
    assert reading["poll_interval_seconds"] == 2.0
    assert reading["last_poll_age_seconds"] >= 140
    assert "chaos" not in json.dumps(reading).lower()


async def test_over_the_wire_a_running_resolver_reads_polling_true(  # type: ignore[no-untyped-def]
    mcp, db_session: AsyncSession, default_tenant
) -> None:
    ac, redis = mcp
    await _resolver_world(redis, polled_seconds_ago=1)
    token = await _token(db_session, default_tenant.id, [Scope.TELEMETRY_READ.value])

    reading = _content(
        await _call(ac, token, "get_consumer_lag", {"consumer_group": "dependency-resolver"})
    )

    assert reading["polling"] is True
    assert reading["poll_interval_seconds"] == 2.0


async def test_over_the_wire_a_static_group_reads_polling_null(  # type: ignore[no-untyped-def]
    mcp, db_session: AsyncSession, default_tenant
) -> None:
    """Nothing polls a recorded constant: unknown, which is not the same as false."""
    ac, redis = mcp
    await redis.set(lag_key("billing-consumer"), 15000)
    token = await _token(db_session, default_tenant.id, [Scope.TELEMETRY_READ.value])

    reading = _content(
        await _call(ac, token, "get_consumer_lag", {"consumer_group": "billing-consumer"})
    )

    assert reading["source"] == "static"
    assert reading["polling"] is None
    assert reading["poll_interval_seconds"] is None


async def test_tools_list_carries_the_verdict_and_says_it_in_one_sentence(  # type: ignore[no-untyped-def]
    mcp, db_session: AsyncSession, default_tenant
) -> None:
    ac, _ = mcp
    token = await _token(db_session, default_tenant.id, [Scope.TELEMETRY_READ.value])
    resp = await ac.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": "1", "method": "tools/list", "params": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    lag = {t["name"]: t for t in resp.json()["result"]["tools"]}["get_consumer_lag"]

    assert {"polling", "poll_interval_seconds"} <= set(lag["outputSchema"]["properties"])
    assert (
        "a dead consumer reads `polling: false` while `lag`, `lag_known`, `source` and "
        "`age_seconds` can all still look healthy (a lag of 0 included), and "
        "`last_poll_age_seconds` is the number behind that verdict."
    ) in lag["description"]


# 2. `get_control_loops` reads the worker's loops


async def test_a_paused_loop_reads_paused_with_its_expiry(  # type: ignore[no-untyped-def]
    mcp, db_session: AsyncSession, default_tenant
) -> None:
    ac, redis = mcp
    await _stranded_world(redis)
    token = await _token(db_session, default_tenant.id, AGENT_SCOPES)

    with patch("app.workers.control_loop_pause.get_settings", return_value=_LAB_ON):
        body = _content(await _call(ac, token, "get_control_loops"))

    loops = {loop["name"]: loop for loop in body["loops"]}
    assert body["total"] == len(body["loops"]) == len(ControlLoopName) == 11
    assert [loop["name"] for loop in body["loops"]] == [m.value for m in ControlLoopName]
    assert body["unknown_reason"] is None

    sweep = loops["resume_unblocked_waiting"]
    assert sweep["paused"] is True
    assert sweep["paused_expires_in_seconds"] == _PAUSE_TTL
    assert sweep["tick_interval_seconds"] == 10.0
    assert sweep["last_run_age_seconds"] >= 140

    # Every other loop is running and unpaused, and says so in the same fields.
    for name, loop in loops.items():
        if name == "resume_unblocked_waiting":
            continue
        assert loop["paused"] is False, name
        assert loop["paused_expires_in_seconds"] is None, name
        assert loop["last_run_age_seconds"] <= 3, name


async def test_an_unpaused_world_reads_unpaused(  # type: ignore[no-untyped-def]
    mcp, db_session: AsyncSession, default_tenant
) -> None:
    ac, redis = mcp
    now = datetime.now(UTC)
    for loop in ControlLoopName:
        await redis.set(loop_run_key(loop), (now - timedelta(seconds=1)).isoformat())
    token = await _token(db_session, default_tenant.id, AGENT_SCOPES)

    with patch("app.workers.control_loop_pause.get_settings", return_value=_LAB_ON):
        body = _content(await _call(ac, token, "get_control_loops"))

    assert all(loop["paused"] is False for loop in body["loops"])
    assert all(loop["paused_expires_in_seconds"] is None for loop in body["loops"])


async def test_the_relay_reading_is_the_relays_own_heartbeat(  # type: ignore[no-untyped-def]
    mcp, db_session: AsyncSession, default_tenant
) -> None:
    """One pass, one record: `get_outbox_status` and this tool read the same stamp."""
    ac, redis = mcp
    stamp = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    await redis.set(RELAY_TICK_KEY, stamp)
    token = await _token(db_session, default_tenant.id, [Scope.TELEMETRY_READ.value])

    body = _content(await _call(ac, token, "get_control_loops"))

    relay = next(loop for loop in body["loops"] if loop["name"] == "outbox_relay")
    assert relay["last_run_at"] is not None
    assert 29 <= relay["last_run_age_seconds"] <= 33


async def test_a_loop_with_no_record_reads_null_never_an_age(  # type: ignore[no-untyped-def]
    mcp, db_session: AsyncSession, default_tenant
) -> None:
    ac, _ = mcp
    token = await _token(db_session, default_tenant.id, [Scope.TELEMETRY_READ.value])

    body = _content(await _call(ac, token, "get_control_loops"))

    assert all(loop["last_run_at"] is None for loop in body["loops"])
    assert all(loop["last_run_age_seconds"] is None for loop in body["loops"])
    assert body["unknown_reason"] is None, "no record is not an unreachable store"


async def test_an_unreachable_store_is_unknown_not_eleven_healthy_loops(  # type: ignore[no-untyped-def]
    mcp, db_session: AsyncSession, default_tenant
) -> None:
    ac, redis = mcp
    redis.fail = True
    token = await _token(db_session, default_tenant.id, [Scope.TELEMETRY_READ.value])

    with patch("app.workers.control_loop_pause.get_settings", return_value=_LAB_ON):
        body = _content(await _call(ac, token, "get_control_loops"))

    assert body["unknown_reason"]
    assert body["total"] == 11
    for loop in body["loops"]:
        assert loop["paused"] is None, loop["name"]
        assert loop["last_run_at"] is None, loop["name"]
        assert loop["last_run_age_seconds"] is None, loop["name"]


# 3. The read scope is enough, and nothing narrower is


async def test_telemetry_read_alone_reads_both(  # type: ignore[no-untyped-def]
    mcp, db_session: AsyncSession, default_tenant
) -> None:
    ac, redis = mcp
    await _stranded_world(redis)
    token = await _token(db_session, default_tenant.id, [Scope.TELEMETRY_READ.value])

    with patch("app.workers.control_loop_pause.get_settings", return_value=_LAB_ON):
        loops = await _call(ac, token, "get_control_loops")
    lag = await _call(ac, token, "get_consumer_lag", {"consumer_group": "dependency-resolver"})

    assert "error" not in loops and "error" not in lag


async def test_a_token_without_telemetry_read_is_refused(  # type: ignore[no-untyped-def]
    mcp, db_session: AsyncSession, default_tenant
) -> None:
    ac, _ = mcp
    token = await _token(db_session, default_tenant.id, [Scope.INCIDENTS_READ.value])

    body = await _call(ac, token, "get_control_loops")

    assert "error" in body
    assert "telemetry:read" in body["error"]["message"]


def test_the_tool_is_a_telemetry_read_with_no_arguments() -> None:
    tool = next(t for t in list_tools() if t.name == "get_control_loops")
    assert tool.required_scope is Scope.TELEMETRY_READ
    assert tool.is_idempotent is False
    schema = tool.input_json_schema()
    assert schema.get("properties", {}) == {}
    assert schema.get("additionalProperties") is False
    out = tool.output_json_schema()
    assert set(out["properties"]) == {"measured_at", "loops", "total", "unknown_reason"}
    assert set(out["$defs"]["ControlLoopState"]["properties"]) == {
        "name",
        "paused",
        "paused_expires_in_seconds",
        "tick_interval_seconds",
        "last_run_at",
        "last_run_age_seconds",
    }
    # No class docstring reaches the pinned schema (plat #210).
    assert "description" not in out
    assert "description" not in out["$defs"]["ControlLoopState"]


# 4. The lab stays invisible while both readings are taken (ADR 0012)


async def test_the_agent_reads_the_fault_and_never_the_lab(  # type: ignore[no-untyped-def]
    mcp, db_session: AsyncSession, default_tenant
) -> None:
    """The chaos rows that RECORD this fault stay withheld from the agent's token; the two
    readings report the state and never who caused it; the evaluator still reads the rows."""
    ac, redis = mcp
    await _stranded_world(redis)
    for tool_name, arguments in (
        ("kill_consumer", {"consumer_group": "dependency-resolver"}),
        (
            "pause_control_loop",
            {"loop_name": "resume_unblocked_waiting", "ttl_seconds": _PAUSE_TTL},
        ),
    ):
        db_session.add(
            AuditLog(
                tenant_id=default_tenant.id,
                action=CHAOS_TOOL_INVOKED_ACTION,
                principal_type=PRINCIPAL_TYPE_SERVICE_ACCOUNT,
                principal_id=uuid.uuid4(),
                resource_type="mcp_tool",
                resource_id=tool_name,
                extra_data={
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "scope_used": Scope.CHAOS_INVOKE.value,
                    "outcome": "success",
                },
            )
        )
    await db_session.flush()
    agent = await _token(db_session, default_tenant.id, AGENT_SCOPES)
    evaluator = await _token(db_session, default_tenant.id, EVALUATOR_SCOPES)

    with patch("app.workers.control_loop_pause.get_settings", return_value=_LAB_ON):
        loops = await _call(ac, agent, "get_control_loops")
    lag = await _call(ac, agent, "get_consumer_lag", {"consumer_group": "dependency-resolver"})
    audit = await _call(ac, agent, "list_audit_events", {"action_prefix": "chaos."})

    for body in (loops, lag, audit):
        serialized = json.dumps(body).lower()
        assert "error" not in body, body.get("error")
        for word in ("chaos", "kill_consumer", "pause_control_loop"):
            assert word not in serialized, f"{word} reached the agent: {serialized[:400]}"
    # The state is there, the actor is not: the loop reads paused, nothing says by whom.
    sweep = next(
        loop for loop in _content(loops)["loops"] if loop["name"] == "resume_unblocked_waiting"
    )
    assert sweep["paused"] is True
    assert set(sweep) == {
        "name",
        "paused",
        "paused_expires_in_seconds",
        "tick_interval_seconds",
        "last_run_at",
        "last_run_age_seconds",
    }
    withheld = _content(audit)
    assert withheld["total"] == 0 and withheld["events"] == []

    # Control: the evaluator, who may fire the lab, reads both rows back.
    seen = _content(
        await _call(ac, evaluator, "list_audit_events", {"action_prefix": "chaos."})
    )
    assert {e["resource_id"] for e in seen["events"]} >= {
        "kill_consumer",
        "pause_control_loop",
    }
