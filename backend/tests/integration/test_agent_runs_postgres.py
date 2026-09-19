"""`agent_runs` on a real Postgres: the policy binds, and JSONB is not a list in Python.

WO-R3-312 / ADR 0035. Two things the SQLite unit tier cannot prove.

**The policy.** `test_rls_enforcement.py` derives its table list from the ORM, so it
already asserts ENABLE + FORCE + `tenant_isolation` on this table with no edit. What it
does not do is read the table through the application's own repository under two tenant
contexts, which is the shape a console actually uses — and the strict predicate
(ADR 0026) means a *missing* `app.tenant_id` denies rather than admits, so a forgotten
`set_config` is a table that reads empty rather than a table that reads everything.

**The column type.** `phase_history` is JSONB here and plain JSON on SQLite, and the
service rebinds the list rather than appending in place — because SQLAlchemy does not
track in-place mutation of a JSON column and an `.append()` is silently dropped. On
SQLite a dropped append can still pass if the object stays in the identity map; a fresh
session against Postgres is what proves the write reached the server.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from app.models.agent_run import AgentRun
from app.models.enums import AgentRunState
from app.models.service_account import ServiceAccount
from app.models.tenant import Tenant
from app.repositories.agent_run import AgentRunRepository
from app.services.agent_run import (
    AgentRunAlreadyFinishedError,
    AgentRunBriefingAlreadyRecordedError,
    AgentRunService,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

try:
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    _HAS_TC = True
except Exception:  # pragma: no cover
    _HAS_TC = False


def _has_docker() -> bool:
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=True)
        return True
    except Exception:  # pragma: no cover - environment-dependent
        return False


pytestmark = pytest.mark.skipif(
    not _HAS_TC or not _has_docker(),
    reason="needs Docker + testcontainers[postgres]",
)

_T0 = datetime(2026, 9, 19, 8, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def pg() -> Any:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


@pytest.fixture(scope="module")
def migrated(pg: Any) -> str:
    """The whole Alembic chain, so the table arrives with its policy rather than from
    `create_all` (which would create it with RLS off and prove nothing)."""
    url = pg.get_connection_url()
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    # Popped, not overridden: `env.py::_get_url` prefers it, so an inherited value would
    # point this fixture's migration at someone else's database.
    env.pop("ALEMBIC_DATABASE_URL", None)
    subprocess.check_call(
        [sys.executable, "-m", "alembic", "-c", str(REPO_ROOT / "alembic.ini"), "upgrade", "head"],
        env=env,
        cwd=REPO_ROOT,
    )
    return str(url)


@pytest_asyncio.fixture
async def engine(migrated: str) -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine(migrated, echo=False)
    try:
        yield eng
    finally:
        await eng.dispose()


def _factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def _session_for(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID | None
) -> AsyncSession:
    """A session scoped to one tenant, or to the platform when `tenant_id is None`.

    The two GUCs are transaction-local on purpose (ADR 0026): the runtime shares one pool
    between requests and workers, so a session-level SET would leak the scope onto the
    next checkout.
    """
    session = factory()
    await session.begin()
    if tenant_id is None:
        await session.execute(
            text("SELECT set_config('app.tenant_scope', 'platform', true)")
        )
    else:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
        )
    return session


async def _seed_tenant(
    factory: async_sessionmaker[AsyncSession], slug: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """`(tenant_id, service_account_id)`, written under declared platform scope."""
    session = await _session_for(factory, None)
    try:
        tenant = Tenant(id=uuid.uuid4(), slug=slug, name=slug, is_active=True)
        session.add(tenant)
        await session.flush()
        sa = ServiceAccount(
            tenant_id=tenant.id,
            name=f"reporter-{uuid.uuid4().hex[:8]}",
            scopes=["agent_runs:write"],
            is_active=True,
        )
        session.add(sa)
        await session.flush()
        ids = (tenant.id, sa.id)
        await session.commit()
        return ids
    finally:
        await session.close()


async def test_a_run_is_invisible_to_another_tenant_under_the_real_policy(
    engine: AsyncEngine,
) -> None:
    """THE RLS assertion for this table, taken through the repository a console uses."""
    factory = _factory(engine)
    tenant_a, sa_a = await _seed_tenant(factory, f"a-{uuid.uuid4().hex[:6]}")
    tenant_b, _ = await _seed_tenant(factory, f"b-{uuid.uuid4().hex[:6]}")

    session = await _session_for(factory, tenant_a)
    try:
        outcome = await AgentRunService(AgentRunRepository(session)).report_run(
            run_id=uuid.uuid4(),
            tenant_id=tenant_a,
            service_account_id=sa_a,
            state=AgentRunState.INVESTIGATING.value,
            at=_T0,
        )
        run_id = outcome.run.id
        await session.commit()
    finally:
        await session.close()

    as_a = await _session_for(factory, tenant_a)
    as_b = await _session_for(factory, tenant_b)
    try:
        repo_a = AgentRunRepository(as_a)
        repo_b = AgentRunRepository(as_b)
        assert await repo_a.get_for_tenant(run_id, tenant_a) is not None
        # Asked for A's run from B's session, naming A's tenant: the app-layer predicate
        # and the policy both refuse, and the second is what a forgotten predicate would
        # be left with.
        assert await repo_b.get_for_tenant(run_id, tenant_a) is None
        rows_b, total_b = await repo_b.list_for_tenant(tenant_b)
        assert (rows_b, total_b) == ([], 0)
        # And the raw statement, with no app-layer filter at all, sees nothing from B.
        leaked = (
            (await as_b.execute(select(AgentRun).where(AgentRun.id == run_id)))
            .scalars()
            .all()
        )
        assert leaked == []
    finally:
        await as_a.close()
        await as_b.close()


async def test_an_unscoped_session_reads_nothing_rather_than_everything(
    engine: AsyncEngine,
) -> None:
    """ADR 0026's direction, on the newest table: silence is refused, not admitted. A
    forgotten `set_config` is an empty page, not a cross-tenant one."""
    factory = _factory(engine)
    tenant, sa = await _seed_tenant(factory, f"c-{uuid.uuid4().hex[:6]}")
    session = await _session_for(factory, tenant)
    try:
        await AgentRunService(AgentRunRepository(session)).report_run(
            run_id=uuid.uuid4(),
            tenant_id=tenant,
            service_account_id=sa,
            state=AgentRunState.TRIAGE.value,
            at=_T0,
        )
        await session.commit()
    finally:
        await session.close()

    bare = _factory(engine)()
    try:
        await bare.begin()
        rows = (await bare.execute(select(AgentRun))).scalars().all()
        assert rows == []
    finally:
        await bare.close()


async def test_the_phase_history_append_reaches_the_server(
    engine: AsyncEngine,
) -> None:
    """The JSONB half: rebinding the list is what makes the append durable, and a fresh
    session is the only thing that can tell a durable write from a live object."""
    factory = _factory(engine)
    tenant, sa = await _seed_tenant(factory, f"d-{uuid.uuid4().hex[:6]}")
    run_id = uuid.uuid4()
    walk = ["triage", "investigating", "investigating", "planning", "resolved"]

    for offset, state in enumerate(walk):
        session = await _session_for(factory, tenant)
        try:
            await AgentRunService(AgentRunRepository(session)).report_run(
                run_id=run_id,
                tenant_id=tenant,
                service_account_id=sa,
                state=state,
                at=_T0 + timedelta(seconds=offset),
            )
            await session.commit()
        finally:
            await session.close()

    fresh = await _session_for(factory, tenant)
    try:
        run = await AgentRunRepository(fresh).get_for_tenant(run_id, tenant)
        assert run is not None
        assert [e["state"] for e in run.phase_history] == [
            "triage",
            "investigating",
            "planning",
            "resolved",
        ]
        assert run.finished_at is not None
        assert isinstance(run.phase_history, list)
    finally:
        await fresh.close()


async def test_the_two_conflicts_hold_across_sessions(engine: AsyncEngine) -> None:
    """Both 409s are about a row someone else may already have read, so they have to
    hold against the stored row rather than against this session's copy."""
    factory = _factory(engine)
    tenant, sa = await _seed_tenant(factory, f"e-{uuid.uuid4().hex[:6]}")
    run_id = uuid.uuid4()

    first = await _session_for(factory, tenant)
    try:
        await AgentRunService(AgentRunRepository(first)).report_run(
            run_id=run_id,
            tenant_id=tenant,
            service_account_id=sa,
            state=AgentRunState.ESCALATED.value,
            at=_T0,
        )
        await AgentRunService(AgentRunRepository(first)).report_briefing(
            run_id=run_id,
            tenant_id=tenant,
            briefing={"final_state": "escalated"},
            prose="Budget spent.",
            at=_T0,
        )
        await first.commit()
    finally:
        await first.close()

    second = await _session_for(factory, tenant)
    try:
        service = AgentRunService(AgentRunRepository(second))
        with pytest.raises(AgentRunAlreadyFinishedError):
            await service.report_run(
                run_id=run_id,
                tenant_id=tenant,
                service_account_id=sa,
                state=AgentRunState.PLANNING.value,
                at=_T0 + timedelta(seconds=1),
            )
        with pytest.raises(AgentRunBriefingAlreadyRecordedError):
            await service.report_briefing(
                run_id=run_id,
                tenant_id=tenant,
                briefing={"final_state": "rewritten"},
                prose=None,
                at=_T0 + timedelta(seconds=1),
            )
    finally:
        await second.close()

    third = await _session_for(factory, tenant)
    try:
        run = await AgentRunRepository(third).get_for_tenant(run_id, tenant)
        assert run is not None
        assert run.briefing is not None
        assert run.briefing["final_state"] == "escalated"
        assert run.briefing["prose"] == "Budget spent."
        assert run.state == "escalated"
    finally:
        await third.close()


async def test_the_table_carries_the_policy_and_the_partial_index(
    engine: AsyncEngine,
) -> None:
    """The migration's own claims, read back off the catalogue: FORCE plus the strict
    predicate, and the partial index the console's poll depends on."""
    session = _factory(engine)()
    try:
        await session.begin()
        forced = (
            await session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
                    " WHERE relname = 'agent_runs'"
                )
            )
        ).one()
        assert forced == (True, True)

        policy = (
            await session.execute(
                text(
                    "SELECT qual FROM pg_policies"
                    " WHERE tablename = 'agent_runs' AND policyname = 'tenant_isolation'"
                )
            )
        ).scalar_one()
        assert "app.tenant_scope" in policy
        assert "nullif" in policy

        indexes = (
            (
                await session.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes WHERE tablename = 'agent_runs'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert any(
            "ix_agent_runs_active" in d and "finished_at IS NULL" in d for d in indexes
        ), indexes
    finally:
        await session.close()
