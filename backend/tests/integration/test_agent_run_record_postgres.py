"""The run record on a real Postgres: the migration lands, and JSONB keeps the ledger.

WO-R3-328 / ADR 0037. Three things the SQLite unit tier cannot prove.

**The migration.** `b6c1d90f4a27` adds seven columns with two server defaults written in
Postgres syntax (`'[]'::jsonb`, `0`). SQLite never runs it, so the only proof that the
columns arrive NOT NULL with a default an existing row can satisfy is an `alembic upgrade
head` against a server — and the same for the down path, which the repo requires every
migration to have and which nothing else here exercises.

**The append that has to be rebound.** `steps` and `verifications` are JSONB here and
plain JSON on SQLite, and the service rebinds the list rather than appending in place,
because SQLAlchemy does not track in-place mutation of a JSON column and an `.append()`
is silently dropped. On SQLite a dropped append can still pass if the object stays in the
identity map; a fresh session against Postgres is what proves the write reached the
server. `phase_history` learned this in WO-R3-312 and two more lists is two more places
to forget it.

**The cap, over a run longer than the cap.** 205 reported steps have to leave 200 stored
and `steps_dropped` at 5, with the oldest gone and the newest kept — on the same column
type the demo will read.

This file connects as the **owner** and asserts nothing about row security: the policy on
this table is proved by `test_agent_runs_postgres.py` (as the non-owner role, which is
load-bearing there) and `test_rls_enforcement.py` derives its table list from the ORM, so
neither needs an edit for a column.
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
from app.services.agent_run import STEPS_CAP, VERIFICATIONS_CAP, AgentRunService
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

_T0 = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)

#: The columns this order adds, with what the server must report for each.
_NEW_COLUMNS = {
    "hypotheses": ("jsonb", "NO"),
    "plan": ("jsonb", "YES"),
    "verification": ("jsonb", "YES"),
    "verifications": ("jsonb", "NO"),
    "steps": ("jsonb", "NO"),
    "steps_dropped": ("integer", "NO"),
    "budget": ("jsonb", "YES"),
}


@pytest.fixture(scope="module")
def pg() -> Any:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


@pytest.fixture(scope="module")
def migrated_url(pg: Any) -> str:
    """The whole Alembic chain, as the owner. The chain rather than `create_all`:
    `create_all` builds the columns from the ORM and would prove nothing about the
    migration that has to add them to a table that already exists."""
    url = str(pg.get_connection_url())
    _alembic(url, "upgrade", "head")
    return url


def _alembic(database_url: str, *args: str) -> None:
    """Run an alembic command against the container. `ALEMBIC_DATABASE_URL` is popped,
    not overridden: `env.py::_get_url` prefers it (ADR 0015), so an inherited value would
    point this fixture's migration somewhere else entirely."""
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env.pop("ALEMBIC_DATABASE_URL", None)
    subprocess.check_call(
        [sys.executable, "-m", "alembic", "-c", str(REPO_ROOT / "alembic.ini"), *args],
        env=env,
        cwd=REPO_ROOT,
    )


@pytest_asyncio.fixture
async def engine(migrated_url: str) -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine(migrated_url, echo=False)
    try:
        yield eng
    finally:
        await eng.dispose()


def _factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def _session(factory: async_sessionmaker[AsyncSession]) -> AsyncSession:
    """A platform-scoped session. The owner is exempt from nothing here — this file makes
    no policy claim — but the scope GUC is what every writer in this platform declares
    (ADR 0026), so the sessions look like the real ones."""
    session = factory()
    await session.begin()
    await session.execute(
        text("SELECT set_config('app.tenant_scope', 'platform', true)")
    )
    return session


async def _seed(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID]:
    session = await _session(factory)
    try:
        tenant = Tenant(
            id=uuid.uuid4(),
            slug=f"rec-{uuid.uuid4().hex[:8]}",
            name="record",
            is_active=True,
        )
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


def _step(seq: int, **over: Any) -> dict[str, Any]:
    step = {
        "seq": seq,
        "kind": "read",
        "tool": "get_consumer_lag",
        "arguments": {"consumer_group": "worker-dispatcher"},
        "result_excerpt": f"lag 42 at sample {seq}",
        "outcome": "success",
        "latency_ms": 12.5,
        "at": (_T0 + timedelta(seconds=seq)).isoformat(),
    }
    step.update(over)
    return step


async def _report(
    factory: async_sessionmaker[AsyncSession],
    *,
    run_id: uuid.UUID,
    tenant_id: uuid.UUID,
    service_account_id: uuid.UUID,
    state: str = AgentRunState.INVESTIGATING.value,
    **fields: Any,
) -> None:
    """One report, in its own session and committed — so the next read is a real read."""
    session = await _session(factory)
    try:
        await AgentRunService(AgentRunRepository(session)).report_run(
            run_id=run_id,
            tenant_id=tenant_id,
            service_account_id=service_account_id,
            state=state,
            at=_T0,
            **fields,
        )
        await session.commit()
    finally:
        await session.close()


async def _read(
    factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID, tenant_id: uuid.UUID
) -> AgentRun:
    session = await _session(factory)
    try:
        run = await AgentRunRepository(session).get_for_tenant(run_id, tenant_id)
        assert run is not None
        return run
    finally:
        await session.close()


async def test_the_migration_adds_the_seven_columns_with_usable_defaults(
    engine: AsyncEngine,
) -> None:
    """The columns arrive, with the types and the nullability the model declares — and
    the three lists arrive with a default an existing row can satisfy, which is the only
    reason `NOT NULL` is addable to a populated table at all."""
    session = _factory(engine)()
    try:
        await session.begin()
        rows = (
            await session.execute(
                text(
                    "SELECT column_name, data_type, is_nullable, column_default"
                    " FROM information_schema.columns"
                    " WHERE table_name = 'agent_runs'"
                    "   AND column_name = ANY(:names)"
                ),
                {"names": list(_NEW_COLUMNS)},
            )
        ).all()
        found = {r.column_name: r for r in rows}

        assert set(found) == set(_NEW_COLUMNS), sorted(found)
        for name, (data_type, nullable) in _NEW_COLUMNS.items():
            assert found[name].data_type == data_type, name
            assert found[name].is_nullable == nullable, name
        for name in ("hypotheses", "verifications", "steps"):
            assert "[]" in (found[name].column_default or ""), name
        assert (found["steps_dropped"].column_default or "").startswith("0")
    finally:
        await session.close()


async def test_the_down_path_drops_them_and_the_up_path_puts_them_back(
    migrated_url: str, engine: AsyncEngine
) -> None:
    """Every migration here must have a real `downgrade()`, and this is the only place it
    runs. Down then straight back up, so the rest of the module sees the schema it
    expects whatever order the tests run in."""

    async def _present() -> set[str]:
        session = _factory(engine)()
        try:
            await session.begin()
            rows = (
                await session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_name = 'agent_runs' AND column_name = ANY(:names)"
                    ),
                    {"names": list(_NEW_COLUMNS)},
                )
            ).all()
            return {r.column_name for r in rows}
        finally:
            await session.close()

    assert await _present() == set(_NEW_COLUMNS)
    try:
        _alembic(migrated_url, "downgrade", "-1")
        assert await _present() == set()
    finally:
        _alembic(migrated_url, "upgrade", "head")
    assert await _present() == set(_NEW_COLUMNS)


async def test_the_step_ledger_survives_a_fresh_session(engine: AsyncEngine) -> None:
    """The rebind assertion, for the two new lists. Each report commits and the read
    happens in a session that never saw the object, so an `.append()` SQLAlchemy dropped
    would show up as a ledger of one."""
    factory = _factory(engine)
    tenant_id, sa_id = await _seed(factory)
    run_id = uuid.uuid4()

    for seq in range(1, 4):
        await _report(
            factory,
            run_id=run_id,
            tenant_id=tenant_id,
            service_account_id=sa_id,
            step=_step(seq),
            verification={"verdict": "not_verified", "attempt": seq, "of": 3},
        )

    run = await _read(factory, run_id, tenant_id)
    assert [s["seq"] for s in run.steps] == [1, 2, 3]
    # The whole entry round-trips, nested object included.
    assert run.steps[0]["arguments"] == {"consumer_group": "worker-dispatcher"}
    assert run.steps[2]["result_excerpt"] == "lag 42 at sample 3"
    assert [v["attempt"] for v in run.verifications] == [1, 2, 3]
    assert run.verification["attempt"] == 3
    assert run.steps_dropped == 0


async def test_a_repeated_seq_does_not_enter_the_ledger_twice(
    engine: AsyncEngine,
) -> None:
    """The reporter is fail-open: it may retry a report whose answer it never saw. On the
    real column type, a retry has to be a no-op rather than a second row in the ledger."""
    factory = _factory(engine)
    tenant_id, sa_id = await _seed(factory)
    run_id = uuid.uuid4()

    for _ in range(3):
        await _report(
            factory,
            run_id=run_id,
            tenant_id=tenant_id,
            service_account_id=sa_id,
            step=_step(7),
        )

    run = await _read(factory, run_id, tenant_id)
    assert [s["seq"] for s in run.steps] == [7]
    assert run.steps_dropped == 0


async def test_a_run_longer_than_the_cap_keeps_the_newest_and_counts_the_rest(
    engine: AsyncEngine,
) -> None:
    """The cap on the column the demo reads. Oldest go first, because the end of a run is
    what an operator is looking at — and `steps_dropped` is what stops a console
    describing the newest 200 calls as the whole run."""
    factory = _factory(engine)
    tenant_id, sa_id = await _seed(factory)
    run_id = uuid.uuid4()
    overshoot = 5

    session = await _session(factory)
    try:
        service = AgentRunService(AgentRunRepository(session))
        for seq in range(1, STEPS_CAP + overshoot + 1):
            await service.report_run(
                run_id=run_id,
                tenant_id=tenant_id,
                service_account_id=sa_id,
                state=AgentRunState.INVESTIGATING.value,
                at=_T0,
                step=_step(seq),
            )
        await session.commit()
    finally:
        await session.close()

    run = await _read(factory, run_id, tenant_id)
    assert len(run.steps) == STEPS_CAP
    assert run.steps_dropped == overshoot
    assert run.steps[0]["seq"] == overshoot + 1, "the oldest went, not the newest"
    assert run.steps[-1]["seq"] == STEPS_CAP + overshoot


async def test_the_verdict_list_is_capped_too(engine: AsyncEngine) -> None:
    factory = _factory(engine)
    tenant_id, sa_id = await _seed(factory)
    run_id = uuid.uuid4()

    session = await _session(factory)
    try:
        service = AgentRunService(AgentRunRepository(session))
        for attempt in range(1, VERIFICATIONS_CAP + 3):
            await service.report_run(
                run_id=run_id,
                tenant_id=tenant_id,
                service_account_id=sa_id,
                state=AgentRunState.VERIFYING.value,
                at=_T0,
                verification={"verdict": "not_verified", "attempt": attempt},
            )
        await session.commit()
    finally:
        await session.close()

    run = await _read(factory, run_id, tenant_id)
    assert len(run.verifications) == VERIFICATIONS_CAP
    assert run.verifications[0]["attempt"] == 3
    assert run.verification["attempt"] == VERIFICATIONS_CAP + 2


async def test_a_reading_is_not_cleared_by_a_report_that_omits_it(
    engine: AsyncEngine,
) -> None:
    """Rule 5 on the real column type: the step-only report that follows a transition
    must leave the reasoning standing. This is the empty-panel failure, from the other
    direction."""
    factory = _factory(engine)
    tenant_id, sa_id = await _seed(factory)
    run_id = uuid.uuid4()

    await _report(
        factory,
        run_id=run_id,
        tenant_id=tenant_id,
        service_account_id=sa_id,
        hypotheses=[
            {"name": "a stalled consumer", "confidence": 0.8, "category": "consumer"},
            {"name": "a slow downstream", "confidence": 0.2},
        ],
        plan={"action_tool": "restart_consumer_group", "target_hypothesis": "a stalled consumer"},
        budget={"tool_calls_used": 3, "tool_calls_max": 13},
    )
    # ...and then a report about one step, carrying none of it.
    await _report(
        factory,
        run_id=run_id,
        tenant_id=tenant_id,
        service_account_id=sa_id,
        step=_step(1),
    )

    run = await _read(factory, run_id, tenant_id)
    assert [h["name"] for h in run.hypotheses] == [
        "a stalled consumer",
        "a slow downstream",
    ]
    assert run.plan["action_tool"] == "restart_consumer_group"
    assert run.budget["tool_calls_used"] == 3
    assert [s["seq"] for s in run.steps] == [1]


async def test_the_new_columns_come_back_empty_rather_than_null(
    engine: AsyncEngine,
) -> None:
    """A run that reported none of this reads as empty lists and nulls, never as NULL
    lists — so a console needs no defensive branch per column."""
    factory = _factory(engine)
    tenant_id, sa_id = await _seed(factory)
    run_id = uuid.uuid4()

    await _report(
        factory,
        run_id=run_id,
        tenant_id=tenant_id,
        service_account_id=sa_id,
        state=AgentRunState.TRIAGE.value,
    )

    session = await _session(factory)
    try:
        run = (
            await session.execute(select(AgentRun).where(AgentRun.id == run_id))
        ).scalar_one()
        assert run.hypotheses == []
        assert run.verifications == []
        assert run.steps == []
        assert run.steps_dropped == 0
        assert run.plan is None
        assert run.verification is None
        assert run.budget is None
    finally:
        await session.close()
