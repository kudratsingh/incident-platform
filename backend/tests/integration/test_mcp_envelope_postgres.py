"""The transaction envelope against a real Postgres (WO-R2-06).

Postgres aborts the whole transaction on a constraint violation, so the audit write after a
failed INSERT is refused and COMMIT degrades to ROLLBACK — only a SAVEPOINT recovers, where
SQLite lets the caught-and-continue version pass. Pinned against `uq_idempotency_scope`.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from app.core.scopes import Scope
from app.dependencies import Principal
from app.mcp import protocol
from app.mcp.registry import (
    ToolContext,
    _restore_for_tests,
    _snapshot_for_tests,
    tool,
)
from app.models.audit import AuditLog
from app.models.base import Base
from app.models.idempotency import IdempotencyRecord
from app.models.service_account import ServiceAccount
from app.models.tenant import Tenant
from app.repositories.audit import AuditRepository
from app.services.idempotency import _hash_arguments
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

try:
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    _HAS_TC = True
except Exception:  # pragma: no cover
    _HAS_TC = False


def _has_docker() -> bool:
    try:
        subprocess.run(
            ["docker", "info"], capture_output=True, timeout=30, check=True
        )
        return True
    except Exception:  # pragma: no cover - environment-dependent
        return False


pytestmark = pytest.mark.skipif(
    not _HAS_TC or not _has_docker(),
    reason="needs Docker + testcontainers[postgres]",
)

TOOL_NAME = "pg_envelope_probe"
CRASH_TOOL_NAME = "pg_envelope_crash"
SIDE_EFFECT_ACTION = "probe.tool_side_effect"


class _In(BaseModel):
    idempotency_key: str


class _Out(BaseModel):
    ok: bool
    marker: str


class _CrashIn(BaseModel):
    pass


class _CrashOut(BaseModel):
    ok: bool


@pytest.fixture(scope="module")
def pg() -> Any:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


@pytest_asyncio.fixture
async def factory(pg: Any) -> Any:
    """Fresh engine per test: asyncpg is bound to its loop."""
    engine = create_async_engine(pg.get_connection_url())
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
def probe_tools() -> Any:
    snapshot = _snapshot_for_tests()

    @tool(
        TOOL_NAME,
        description="Tier-1 shaped idempotent action. Test-only.",
        input_model=_In,
        output_model=_Out,
        required_scope=Scope.ACTIONS_EXECUTE,
        is_idempotent=True,
    )
    async def _probe(inp: _In, ctx: ToolContext) -> _Out:
        return _Out(ok=True, marker="fresh-execution")

    @tool(
        CRASH_TOOL_NAME,
        description="Writes a row, then raises. Test-only.",
        input_model=_CrashIn,
        output_model=_CrashOut,
        required_scope=Scope.ACTIONS_EXECUTE,
    )
    async def _crash(inp: _CrashIn, ctx: ToolContext) -> _CrashOut:
        await AuditRepository(ctx.db).log(
            SIDE_EFFECT_ACTION,
            tenant_id=ctx.principal.tenant_id,
            resource_type="mcp_tool",
        )
        raise RuntimeError("simulated tool crash")

    yield
    _restore_for_tests(snapshot)


async def _seed(factory: Any) -> Principal:
    tenant_id = uuid.uuid4()
    sa_id = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            session.add(
                Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[:8]}", name="envelope")
            )
    # `audit_logs.principal_id` is a plain UUID column by design, so the
    # service account never has to be a row for the audit path to work.
    return Principal(
        kind="service_account",
        tenant_id=tenant_id,
        service_account=ServiceAccount(id=sa_id, tenant_id=tenant_id),
        scopes=frozenset({Scope.ACTIONS_EXECUTE.value}),
    )


async def _rows(factory: Any, action: str) -> list[AuditLog]:
    async with factory() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.action == action)
        )
        return list(result.scalars().all())


async def _call(factory: Any, principal: Principal, name: str, arguments: Any) -> Any:
    """One request, one transaction — the exact shape of `get_db`."""
    from app.mcp.handlers import handle_tools_call

    async with factory() as session:
        async with session.begin():
            ctx = ToolContext(db=session, redis=object(), principal=principal)  # type: ignore[arg-type]
            return await handle_tools_call(
                "1", {"name": name, "arguments": arguments}, ctx=ctx
            )


async def test_store_collision_keeps_the_audit_row_committed(
    factory: Any, probe_tools: Any
) -> None:
    principal = await _seed(factory)
    args = {"idempotency_key": "pg-expired-1"}

    # An expired record squatting on the unique key. `lookup` treats it as
    # absent, so the tool runs — and `store()` then collides with it.
    async with factory() as session:
        async with session.begin():
            session.add(
                IdempotencyRecord(
                    id=uuid.uuid4(),
                    tenant_id=principal.tenant_id,
                    principal_id=principal.id,
                    tool_name=TOOL_NAME,
                    idempotency_key="pg-expired-1",
                    arguments_hash=_hash_arguments(args),
                    response_json={"ok": True, "marker": "stale"},
                    expires_at=datetime.now(UTC) - timedelta(hours=1),
                )
            )

    response = await _call(factory, principal, TOOL_NAME, args)

    assert response.error is None, response.error
    rows = await _rows(factory, "agent.tool_invoked")
    assert len(rows) == 1, (
        "the success audit row did not survive the store() collision"
    )
    assert rows[0].extra_data is not None
    assert rows[0].extra_data["outcome"] == "success"
    assert rows[0].extra_data["tool_name"] == TOOL_NAME


async def test_crashed_tool_rolls_back_its_writes_but_not_its_audit_row(
    factory: Any, probe_tools: Any
) -> None:
    principal = await _seed(factory)

    response = await _call(factory, principal, CRASH_TOOL_NAME, {})

    assert response.error is not None
    assert response.error.code == protocol.JSONRPC_INTERNAL_ERROR

    assert await _rows(factory, SIDE_EFFECT_ACTION) == [], (
        "the crashed tool's own write was committed"
    )
    rows = await _rows(factory, "agent.tool_invoked")
    assert len(rows) == 1, "the crashed call left no audit row"
    assert rows[0].extra_data is not None
    assert rows[0].extra_data["outcome"] == "error"


# R2-27 — the claim is atomic, so a retried tool call cannot 500 after taking effect.
# Postgres-only again: SQLite serialises onto one connection, so the race cannot exist.

CONCURRENT_TOOL_NAME = "pg_claim_probe"


class _SlowIn(BaseModel):
    idempotency_key: str


class _SlowOut(BaseModel):
    ok: bool
    execution: int


@pytest.fixture
def claim_probe_tool() -> Any:
    """Holds its claim while a second caller arrives; counts executions."""
    snapshot = _snapshot_for_tests()
    state: dict[str, Any] = {
        "executions": 0,
        "started": asyncio.Event(),
        "proceed": asyncio.Event(),
    }

    @tool(
        CONCURRENT_TOOL_NAME,
        description="Counts executions, waits to be released. Test-only.",
        input_model=_SlowIn,
        output_model=_SlowOut,
        required_scope=Scope.ACTIONS_EXECUTE,
        is_idempotent=True,
    )
    async def _slow(inp: _SlowIn, ctx: ToolContext) -> _SlowOut:
        state["executions"] += 1
        state["started"].set()
        await state["proceed"].wait()
        return _SlowOut(ok=True, execution=state["executions"])

    yield state
    _restore_for_tests(snapshot)


async def test_concurrent_same_key_calls_execute_once_and_replay(
    factory: Any, claim_probe_tool: Any
) -> None:
    """Same Idempotency-Key twice: one execution, one cached replay, no 500. The
    second caller's `ON CONFLICT DO NOTHING` blocks on the first's uncommitted
    reservation row, so it cannot miss the cache and run the action too."""
    state = claim_probe_tool
    principal = await _seed(factory)
    args = {"idempotency_key": "pg-concurrent-1"}

    first = asyncio.create_task(
        _call(factory, principal, CONCURRENT_TOOL_NAME, args)
    )
    # The first caller now holds the claim and is parked inside the tool.
    await asyncio.wait_for(state["started"].wait(), timeout=10)
    second = asyncio.create_task(
        _call(factory, principal, CONCURRENT_TOOL_NAME, args)
    )
    # Give the second caller time to reach the claim and block on it.
    await asyncio.sleep(0.5)
    state["proceed"].set()

    first_response, second_response = await asyncio.wait_for(
        asyncio.gather(first, second), timeout=30
    )

    assert first_response.error is None, first_response.error
    assert second_response.error is None, second_response.error
    assert state["executions"] == 1, (
        f"the action ran {state['executions']} times for one key"
    )

    # Both callers answer with the one recorded outcome.
    payloads = [
        json.loads(r.result["content"][0]["text"])
        for r in (first_response, second_response)
    ]
    assert payloads[0] == payloads[1] == {"ok": True, "execution": 1}

    # Exactly one record, and exactly one success audit row: the replay
    # is audited as a call, but it is not a second execution.
    async with factory() as session:
        records = (
            await session.execute(select(IdempotencyRecord))
        ).scalars().all()
    assert len(records) == 1
    assert records[0].response_json == {"ok": True, "execution": 1}


CRASH_CLAIM_TOOL_NAME = "pg_claim_crash"


@pytest.fixture
def crashing_claim_tool() -> Any:
    """Tier-1 shaped (so it takes a claim) and always raises."""
    snapshot = _snapshot_for_tests()

    @tool(
        CRASH_CLAIM_TOOL_NAME,
        description="Idempotent action that raises. Test-only.",
        input_model=_SlowIn,
        output_model=_SlowOut,
        required_scope=Scope.ACTIONS_EXECUTE,
        is_idempotent=True,
    )
    async def _crash_claim(inp: _SlowIn, ctx: ToolContext) -> _SlowOut:
        raise RuntimeError("simulated tool crash after the claim")

    yield
    _restore_for_tests(snapshot)


async def test_a_failed_tool_releases_its_claim_for_a_later_retry(
    factory: Any, crashing_claim_tool: Any
) -> None:
    """A claim must not outlive a failed call: the envelope commits on a tool error
    to keep the `outcome=error` row (#154), so the claim is released explicitly."""
    principal = await _seed(factory)

    response = await _call(
        factory,
        principal,
        CRASH_CLAIM_TOOL_NAME,
        {"idempotency_key": "pg-crash-release-1"},
    )
    assert response.error is not None

    async with factory() as session:
        records = (
            await session.execute(select(IdempotencyRecord))
        ).scalars().all()
    assert records == [], "a failed call left its claim behind"
