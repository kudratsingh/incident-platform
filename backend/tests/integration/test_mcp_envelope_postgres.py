"""The transaction envelope against a real Postgres (WO-R2-06).

Why this cannot live in the SQLite tier: Postgres aborts the *entire*
transaction on a constraint violation. Every statement after the failed
INSERT — including the audit write that is the whole point of the path —
is refused with `current transaction is aborted`, and the eventual COMMIT
silently degrades to a ROLLBACK, so a handler that merely catches the
`IntegrityError` still loses the audit row without seeing an error.
SQLite is far more forgiving and will happily let the caught-and-continue
version pass. Only a SAVEPOINT gets the transaction back to a committable
state, and only a real server can demonstrate that.

Two facts pinned here, both against the live `uq_idempotency_scope`
constraint and the real `agent.tool_invoked` rows `evals/guards.py`
grades on:

  1. An idempotency `store()` collision returns a JSON-RPC envelope and
     the success audit row for the action that executed is *committed*.
  2. A crashed tool's own writes are rolled back while its
     `outcome=error` audit row is committed.

Skipped automatically when Docker / testcontainers isn't available.
"""

from __future__ import annotations

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
    """Fresh engine per test — asyncpg connections are bound to the loop
    that opened them, and pytest-asyncio gives each test its own."""
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
