"""`agent_runs` on a real Postgres: the policy binds, and JSONB is not a list in Python.

WO-R3-312 / ADR 0035. Two things the SQLite unit tier cannot prove.

**The policy.** `test_rls_enforcement.py` derives its table list from the ORM, so it
already asserts ENABLE + FORCE + `tenant_isolation` on this table with no edit. What it
does not do is read the table through the application's own repository and service layer
under two tenant contexts, which is the shape a console actually uses — and the strict
predicate (ADR 0026) means a *missing* `app.tenant_id` denies rather than admits, so a
forgotten `set_config` is a table that reads empty rather than a table that reads
everything.

> **Every RLS assertion below runs as the non-owner `incident_app` role, and that is
> load-bearing.** A **superuser bypasses row security unconditionally** — `FORCE ROW
> LEVEL SECURITY` binds the table *owner*, not a superuser — and the testcontainers
> Postgres user is a superuser. The first version of this file connected as it and
> reported that tenant B could read tenant A's run, which was true of the session and
> false of the policy: those assertions would have passed against a table with no policy
> at all. `test_rls_enforcement.py` had this right from the start ("Connect as the
> non-superuser app role — RLS now applies"); this file now follows it, which is why the
> fixture runs `db_bootstrap` to give that role a password before anything connects.

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
from dataclasses import dataclass
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

#: The password `db_bootstrap` gives the runtime role for this container's lifetime.
_APP_PASSWORD = "app_pw"

#: The tenant tables carrying the standard strict predicate. `deploy_markers` is
#: deliberately absent — its policy has an extra `tenant_id IS NULL` disjunct
#: (ADR 0015), so it is the one table `agent_runs` must NOT match.
_STANDARD_POLICY_TABLES = [
    "jobs",
    "audit_logs",
    "alerts",
    "job_triages",
    "service_accounts",
]


@dataclass(frozen=True)
class RlsDb:
    """The two URLs, kept apart on purpose: the owner migrates, the app role is
    bound by the policies."""

    owner_url: str
    app_url: str


@pytest.fixture(scope="module")
def pg() -> Any:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


@pytest.fixture(scope="module")
def rls_db(pg: Any) -> RlsDb:
    """Whole Alembic chain as the owner, then `db_bootstrap` so `incident_app` can log in.

    The chain rather than `create_all`: metadata knows nothing about policies, so a
    `create_all` table would arrive with RLS off and every assertion below would pass
    for the wrong reason.
    """
    owner_url = str(pg.get_connection_url())
    _alembic(owner_url, "upgrade", "head")
    _run_db_bootstrap(owner_url, _APP_PASSWORD)

    host = pg.get_container_host_ip()
    port = pg.get_exposed_port(5432)
    return RlsDb(
        owner_url=owner_url,
        app_url=(
            f"postgresql+asyncpg://incident_app:{_APP_PASSWORD}"
            f"@{host}:{port}/{pg.dbname}"
        ),
    )


def _alembic(database_url: str, *args: str) -> None:
    """Run an alembic command against the container. `ALEMBIC_DATABASE_URL` is popped,
    not overridden: `env.py::_get_url` prefers it (ADR 0015), so an inherited value
    would point this fixture's migration somewhere else entirely."""
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env.pop("ALEMBIC_DATABASE_URL", None)
    subprocess.check_call(
        [sys.executable, "-m", "alembic", "-c", str(REPO_ROOT / "alembic.ini"), *args],
        env=env,
        cwd=REPO_ROOT,
    )


def _run_db_bootstrap(database_url: str, password: str) -> None:
    """The real boot-time password sync, via `python -m app.core.db_bootstrap`.

    b8e4a1c92f35 creates `incident_app` with LOGIN and no password on purpose, so
    without this step nothing can connect as it and the whole point of this file is
    unreachable.
    """
    env = os.environ.copy()
    env["ALEMBIC_DATABASE_URL"] = database_url
    env.pop("DATABASE_URL", None)
    env["INCIDENT_APP_DB_PASSWORD"] = password
    env["PYTHONPATH"] = str(REPO_ROOT / "backend")
    subprocess.check_call(
        [sys.executable, "-m", "app.core.db_bootstrap"],
        env=env,
        cwd=REPO_ROOT,
    )


@pytest_asyncio.fixture
async def engine(rls_db: RlsDb) -> AsyncGenerator[AsyncEngine, None]:
    """The **non-owner** engine. Every test in this file uses it, because a superuser
    or owner-exempt connection cannot observe a policy."""
    eng = create_async_engine(rls_db.app_url, echo=False)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def owner_engine(rls_db: RlsDb) -> AsyncGenerator[AsyncEngine, None]:
    """Catalogue reads only (`pg_class`, `pg_policies`, `pg_indexes`)."""
    eng = create_async_engine(rls_db.owner_url, echo=False)
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

    Both GUCs are transaction-local on purpose (ADR 0026): the runtime shares one pool
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
    """`(tenant_id, service_account_id)`, written under declared platform scope.

    The honest path: a mixed-tenant script declares `app.tenant_scope = 'platform'`
    rather than being admitted for having forgotten (ADR 0026), and this runs as the
    app role, so the WITH CHECK side of the policy is exercised on the way in.
    """
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


async def test_the_app_role_is_not_exempt_from_row_security(
    engine: AsyncEngine,
) -> None:
    """The premise every other test here rests on, asserted rather than assumed.

    A superuser bypasses RLS and an owner is exempt without FORCE, so a session that is
    either would make the isolation tests below vacuous — they would pass against a
    table with no policy. This is the guard that would have caught the first version of
    this file, which connected as the container's superuser.
    """
    session = _factory(engine)()
    try:
        await session.begin()
        row = (
            await session.execute(
                text(
                    "SELECT current_user AS who,"
                    " current_setting('is_superuser') = 'on' AS superuser,"
                    " (SELECT rolbypassrls FROM pg_roles"
                    "  WHERE rolname = current_user) AS bypassrls,"
                    " (SELECT rolname FROM pg_roles"
                    "  WHERE oid = (SELECT relowner FROM pg_class"
                    "               WHERE relname = 'agent_runs')) = current_user"
                    "     AS owner"
                )
            )
        ).one()
        assert row.who == "incident_app", row.who
        assert row.superuser is False, "a superuser bypasses RLS; this test proves nothing"
        assert row.bypassrls is False, "BYPASSRLS makes every policy inert"
        assert row.owner is False, "the owner is exempt unless FORCE is set"
    finally:
        await session.close()


async def test_a_run_is_invisible_to_another_tenant_under_the_real_policy(
    engine: AsyncEngine,
) -> None:
    """THE RLS assertion for this table, taken through the repository a console uses,
    on a connection the policy can actually constrain."""
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
        assert rows == [], "an unscoped read must see nothing"
    finally:
        await bare.close()

    # And the declared-platform-scope path does span tenants, so the assertion above is
    # about the missing declaration rather than about an empty table.
    scoped = await _session_for(factory, None)
    try:
        rows = (await scoped.execute(select(AgentRun))).scalars().all()
        assert rows, "declared platform scope must see the row"
    finally:
        await scoped.close()


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


async def test_the_policy_is_byte_identical_to_every_other_tenant_tables(
    owner_engine: AsyncEngine,
) -> None:
    """The migration's central claim, asserted against the database's own deparse.

    Compared to the other tables' policies rather than to a substring of the SQL that
    created it. Two reasons. The predicate mentions no table name — only `tenant_id` and
    the two GUCs — so equality is exact and a drifted copy shows up as a diff rather
    than as a passing `in` check. And the first version of this test looked for the
    lower-case `nullif` it had written, which Postgres deparses as `NULLIF`: the
    assertion failed on a policy that was correct, which is the worst way for a security
    test to be wrong.
    """
    session = _factory(owner_engine)()
    try:
        await session.begin()
        rows = (
            await session.execute(
                text(
                    "SELECT tablename, qual, with_check FROM pg_policies"
                    " WHERE policyname = 'tenant_isolation'"
                    "   AND tablename = ANY(CAST(:names AS text[]))"
                ),
                {"names": [*_STANDARD_POLICY_TABLES, "agent_runs"]},
            )
        ).all()
        by_table = {r.tablename: (r.qual, r.with_check) for r in rows}

        assert "agent_runs" in by_table, "agent_runs has no tenant_isolation policy"
        # Anti-vacuity: the comparison set has to be populated, or one row compared to
        # itself would pass.
        assert set(_STANDARD_POLICY_TABLES) <= set(by_table), sorted(by_table)
        assert len(set(by_table.values())) == 1, (
            "agent_runs' policy differs from the other tenant tables': "
            f"{ {t: v for t, v in by_table.items()} }"
        )
        qual, with_check = by_table["agent_runs"]
        # USING and WITH CHECK are the same predicate, as the migration wrote them.
        assert qual == with_check
        # And it really is the strict form, not the pre-ADR-0026 permissive one, whose
        # give-away is the `current_setting(...) IS NULL` disjunct that admitted every
        # unscoped statement. (`NULLIF` does not contain the substring `IS NULL`.)
        assert "app.tenant_scope" in qual
        assert "IS NULL" not in qual.upper(), qual
    finally:
        await session.close()


async def test_the_table_carries_force_and_the_partial_index(
    owner_engine: AsyncEngine,
) -> None:
    """The migration's other two claims: FORCE, so the owner is bound too, and the
    partial index the console's two-second poll depends on."""
    session = _factory(owner_engine)()
    try:
        await session.begin()
        posture = (
            await session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
                    " WHERE relname = 'agent_runs'"
                )
            )
        ).one()
        assert tuple(posture) == (True, True)

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
