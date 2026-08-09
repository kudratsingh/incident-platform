"""Postgres row-level security enforcement test.

Boots a real Postgres in a container, runs the full Alembic migration
chain (including both RLS migrations), then proves:

  1. With `app.tenant_id` set to tenant A, queries see only A's rows.
  2. With it set to B, only B's rows are visible.
  3. With it unset, *all* rows are visible — this is the bootstrap escape
     hatch the policy was deliberately written with (workers and
     migrations don't carry a tenant context).
  4. Posture: every tenant table has RLS both ENABLEd and FORCEd. FORCE
     is what makes the policies bind the table *owner* — production
     connects as the RDS master, which owns every table and is otherwise
     exempt (F1-01).
  5. Tenant tables created after the first RLS migration (alerts as the
     probe) are isolated too (F1-05).
  6. deploy_markers rows with tenant_id NULL (platform-wide deploys)
     stay visible from a tenant-scoped session — the policy variant that
     keeps get_deploy_history working under service-account contexts.
  7. audit_logs is immutable at the DB layer: UPDATE/DELETE from a
     fully-granted non-owner role are silent no-ops — command tag
     'UPDATE 0'/'DELETE 0', not an error (F1-07).
  8. Deleting a job still nulls audit_logs.job_id via the FK's ON DELETE
     SET NULL: referential-integrity actions bypass RLS, so the
     restrictive deny policies don't break scripts/reset_eval_state.py.

The third assertion is intentional: it documents the trade-off the
migration was written under. If a future change tightens the policy
(removes the IS NULL escape), this test will fail loudly and force the
author to migrate workers + relays to set the variable per transaction.

Skipped automatically when Docker / testcontainers isn't available so the
rest of the suite still runs.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

try:
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    _HAS_TC = True
except Exception:  # pragma: no cover
    _HAS_TC = False

pytestmark = pytest.mark.skipif(
    not _HAS_TC or not os.environ.get("RUN_RLS_TEST"),
    reason="set RUN_RLS_TEST=1 and install Docker + testcontainers[postgres] to run",
)

# Every tenant-scoped table under RLS: the six from c4f8e9a52340 plus the
# five from the FORCE-and-full-coverage migration. Mirrors the policy-table
# lists in those two migration files (the unit gate test_rls_coverage.py
# keeps the lists themselves honest against the ORM metadata).
ALL_TENANT_RLS_TABLES = [
    "jobs",
    "audit_logs",
    "outbox_events",
    "job_events",
    "sagas",
    "job_triages",
    "incident_summaries",
    "service_accounts",
    "alerts",
    "idempotency_records",
    "deploy_markers",
]


@pytest.fixture(scope="module")
def pg() -> Any:
    # driver="asyncpg" so get_connection_url() emits postgresql+asyncpg://
    # (asyncpg is a main dependency; psycopg2 is not installed).
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


def _run_migrations(database_url: str) -> None:
    """Run alembic upgrade head against the container.

    Uses the current interpreter and an absolute -c path so the call
    works regardless of cwd and PATH; env.py routes on the URL's dialect
    (postgresql+asyncpg here, so the async engine path).
    """
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(REPO_ROOT / "alembic.ini"),
            "upgrade",
            "head",
        ],
        env=env,
        cwd=REPO_ROOT,
    )


@dataclass(frozen=True)
class RlsDb:
    superuser_dsn: str
    app_dsn: str


@pytest.fixture(scope="module")
def rls_db(pg: Any) -> RlsDb:
    """Migrated database plus a non-owner `app_role`, set up once per module.

    Plain roles (non-superuser, non-owner) never bypass RLS, so the
    policies actually apply to `app_role` sessions. The superuser DSN is
    for fixtures/verification only — superusers bypass RLS even under
    FORCE, which is exactly why production posture matters (the RDS
    master is an *owner*, not a superuser, so FORCE does bind it).
    """
    import asyncpg

    host = pg.get_container_host_ip()
    port = pg.get_exposed_port(5432)
    superuser_dsn = f"postgresql://{pg.username}:{pg.password}@{host}:{port}/{pg.dbname}"
    app_dsn = f"postgresql://app_role:app_pw@{host}:{port}/{pg.dbname}"
    _run_migrations(pg.get_connection_url())

    async def _setup() -> None:
        sup = await asyncpg.connect(superuser_dsn)
        try:
            # Non-superuser role with full DML on the tables the tests
            # touch. Plain users don't bypass RLS, so the policies enforce.
            await sup.execute("CREATE ROLE app_role LOGIN PASSWORD 'app_pw'")
            await sup.execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE "
                "ON jobs, tenants, users, alerts, deploy_markers, audit_logs "
                "TO app_role"
            )
        finally:
            await sup.close()

    asyncio.run(_setup())
    return RlsDb(superuser_dsn=superuser_dsn, app_dsn=app_dsn)


async def _create_tenant(sup: Any, slug: str) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    await sup.execute(
        "INSERT INTO tenants (id, slug, name, is_active) VALUES ($1, $2, $3, true)",
        tenant_id,
        slug,
        slug,
    )
    return tenant_id


async def test_rls_isolates_tenants(rls_db: RlsDb) -> None:
    """Two tenants insert jobs. With app.tenant_id set, each sees only its own.

    Connects as the non-owner app role so the policy actually applies.
    """
    import asyncpg

    sup = await asyncpg.connect(rls_db.superuser_dsn)
    try:
        tenant_a = await _create_tenant(sup, "tenant-a")
        tenant_b = await _create_tenant(sup, "tenant-b")
        user_a = uuid.uuid4()
        user_b = uuid.uuid4()
        await sup.execute(
            "INSERT INTO users (id, tenant_id, email, hashed_password, role, is_active) "
            "VALUES ($1, $2, $3, 'x', 'user', true), ($4, $5, $6, 'x', 'user', true)",
            user_a, tenant_a, "a@a.test", user_b, tenant_b, "b@b.test",
        )
        # retry_count / max_retries are NOT NULL without server defaults
        # (the defaults are ORM-side), so the raw INSERT must supply them.
        await sup.execute(
            "INSERT INTO jobs (id, tenant_id, user_id, type, status, priority, "
            "                  retry_count, max_retries, payload) "
            "VALUES ($1, $2, $3, 'csv_upload', 'pending', 5, 0, 3, '{}'::jsonb), "
            "       ($4, $5, $6, 'csv_upload', 'pending', 5, 0, 3, '{}'::jsonb)",
            uuid.uuid4(), tenant_a, user_a,
            uuid.uuid4(), tenant_b, user_b,
        )
    finally:
        await sup.close()

    # Connect as the non-superuser app role — RLS now applies.
    app = await asyncpg.connect(rls_db.app_dsn)
    try:
        async with app.transaction():
            await app.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_a))
            rows = await app.fetch("SELECT tenant_id FROM jobs")
            assert len(rows) == 1
            assert rows[0]["tenant_id"] == tenant_a

        async with app.transaction():
            await app.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_b))
            rows = await app.fetch("SELECT tenant_id FROM jobs")
            assert len(rows) == 1
            assert rows[0]["tenant_id"] == tenant_b

        # Unset — bootstrap escape hatch lets workers/migrations see all rows.
        async with app.transaction():
            rows = await app.fetch("SELECT tenant_id FROM jobs")
            assert len(rows) == 2
    finally:
        await app.close()


async def test_rls_enabled_and_forced_on_all_tenant_tables(rls_db: RlsDb) -> None:
    """Every tenant table must have RLS ENABLEd *and* FORCEd (F1-01).

    Without FORCE the table owner is exempt — and production connects as
    the RDS master, which owns every table — so ENABLE alone leaves RLS
    inert exactly where it matters.
    """
    import asyncpg

    sup = await asyncpg.connect(rls_db.superuser_dsn)
    try:
        rows = await sup.fetch(
            "SELECT relname, relrowsecurity, relforcerowsecurity "
            "FROM pg_class WHERE relkind = 'r' AND relname = ANY($1::text[])",
            ALL_TENANT_RLS_TABLES,
        )
        by_name = {r["relname"]: r for r in rows}
        assert set(by_name) == set(ALL_TENANT_RLS_TABLES)
        bad_posture = {
            name: {
                "enabled": row["relrowsecurity"],
                "forced": row["relforcerowsecurity"],
            }
            for name, row in sorted(by_name.items())
            if not (row["relrowsecurity"] and row["relforcerowsecurity"])
        }
        assert bad_posture == {}, f"tables without ENABLE+FORCE RLS: {bad_posture}"
    finally:
        await sup.close()


async def test_alerts_isolated_between_tenants(rls_db: RlsDb) -> None:
    """alerts (created after c4f8e9a52340) must be tenant-isolated too (F1-05)."""
    import asyncpg

    sup = await asyncpg.connect(rls_db.superuser_dsn)
    try:
        tenant_a = await _create_tenant(sup, "alerts-a")
        tenant_b = await _create_tenant(sup, "alerts-b")
        alert_a = uuid.uuid4()
        alert_b = uuid.uuid4()
        await sup.execute(
            "INSERT INTO alerts (id, tenant_id, severity, source, title) "
            "VALUES ($1, $2, 'warning', 'chaos:manual', 'A alert'), "
            "       ($3, $4, 'warning', 'chaos:manual', 'B alert')",
            alert_a, tenant_a, alert_b, tenant_b,
        )
    finally:
        await sup.close()

    app = await asyncpg.connect(rls_db.app_dsn)
    try:
        async with app.transaction():
            await app.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_a))
            rows = await app.fetch("SELECT id FROM alerts")
            assert {r["id"] for r in rows} == {alert_a}, (
                "tenant-A session must see exactly A's alert, never B's"
            )
    finally:
        await app.close()


async def test_deploy_markers_null_tenant_rows_visible_in_tenant_scope(rls_db: RlsDb) -> None:
    """deploy_markers keeps platform-wide (tenant_id NULL) rows visible.

    Deploys are platform-wide today (tenant_id is nullable by design), and
    get_deploy_history runs under a service-account tenant context. The
    standard tenant_isolation shape would hide every NULL row and silently
    degrade that MCP tool to its env-var fallback — hence the policy
    variant with `OR tenant_id IS NULL`. Tenant-scoped rows of *other*
    tenants must still be hidden.
    """
    import asyncpg

    sup = await asyncpg.connect(rls_db.superuser_dsn)
    try:
        tenant_a = await _create_tenant(sup, "deploy-a")
        tenant_b = await _create_tenant(sup, "deploy-b")
        marker_null = uuid.uuid4()
        marker_b = uuid.uuid4()
        await sup.execute(
            "INSERT INTO deploy_markers (id, tenant_id, version, environment) "
            "VALUES ($1, NULL, 'v1.0.0', 'test'), ($2, $3, 'v1.0.1', 'test')",
            marker_null, marker_b, tenant_b,
        )
    finally:
        await sup.close()

    app = await asyncpg.connect(rls_db.app_dsn)
    try:
        async with app.transaction():
            await app.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_a))
            rows = await app.fetch("SELECT id FROM deploy_markers")
            ids = {r["id"] for r in rows}
            assert marker_null in ids, (
                "platform-wide (tenant_id NULL) deploy markers must stay visible "
                "under a tenant-scoped session"
            )
            assert marker_b not in ids, (
                "another tenant's deploy marker must not be visible"
            )
    finally:
        await app.close()


async def test_audit_logs_update_delete_are_silent_noops(rls_db: RlsDb) -> None:
    """audit_logs is immutable at the DB layer (F1-07).

    The RESTRICTIVE deny policies make rows invisible to UPDATE/DELETE, so
    a fully-granted non-owner role gets command tag 'UPDATE 0'/'DELETE 0'
    — a silent no-op, NOT an error. (The loud insufficient_privilege
    variant arrives with the WO-P2-03 grant revoke.)
    """
    import asyncpg

    sup = await asyncpg.connect(rls_db.superuser_dsn)
    try:
        tenant = await _create_tenant(sup, "audit-imm")
        audit_id = uuid.uuid4()
        await sup.execute(
            "INSERT INTO audit_logs (id, tenant_id, action) VALUES ($1, $2, 'job.created')",
            audit_id, tenant,
        )
    finally:
        await sup.close()

    app = await asyncpg.connect(rls_db.app_dsn)
    try:
        async with app.transaction():
            # Scope to the row's own tenant: the permissive tenant_isolation
            # policy admits the row, so what blocks the write is precisely
            # the restrictive deny policy.
            await app.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
            tag = await app.execute(
                "UPDATE audit_logs SET action = 'tampered' WHERE id = $1", audit_id
            )
            assert tag == "UPDATE 0", f"audit_logs UPDATE must be a no-op, got {tag!r}"
            tag = await app.execute("DELETE FROM audit_logs WHERE id = $1", audit_id)
            assert tag == "DELETE 0", f"audit_logs DELETE must be a no-op, got {tag!r}"
    finally:
        await app.close()

    sup = await asyncpg.connect(rls_db.superuser_dsn)
    try:
        row = await sup.fetchrow("SELECT action FROM audit_logs WHERE id = $1", audit_id)
        assert row is not None and row["action"] == "job.created"
    finally:
        await sup.close()


async def test_job_delete_still_nulls_audit_fk_via_ri_bypass(rls_db: RlsDb) -> None:
    """Deleting a job must still SET NULL audit_logs.job_id.

    Referential-integrity actions bypass row security, so the restrictive
    deny policies on audit_logs must not block the FK's ON DELETE SET NULL
    — this is what keeps scripts/reset_eval_state.py (which deletes jobs
    and users) working. A trigger-based immutability guard would break it.
    """
    import asyncpg

    sup = await asyncpg.connect(rls_db.superuser_dsn)
    try:
        tenant = await _create_tenant(sup, "audit-fk")
        user_id = uuid.uuid4()
        await sup.execute(
            "INSERT INTO users (id, tenant_id, email, hashed_password, role, is_active) "
            "VALUES ($1, $2, 'fk@fk.test', 'x', 'user', true)",
            user_id, tenant,
        )
        job_id = uuid.uuid4()
        await sup.execute(
            "INSERT INTO jobs (id, tenant_id, user_id, type, status, priority, "
            "                  retry_count, max_retries, payload) "
            "VALUES ($1, $2, $3, 'csv_upload', 'pending', 5, 0, 3, '{}'::jsonb)",
            job_id, tenant, user_id,
        )
        audit_id = uuid.uuid4()
        await sup.execute(
            "INSERT INTO audit_logs (id, tenant_id, job_id, action) "
            "VALUES ($1, $2, $3, 'job.created')",
            audit_id, tenant, job_id,
        )
    finally:
        await sup.close()

    app = await asyncpg.connect(rls_db.app_dsn)
    try:
        async with app.transaction():
            await app.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
            tag = await app.execute("DELETE FROM jobs WHERE id = $1", job_id)
            assert tag == "DELETE 1", f"job delete must succeed, got {tag!r}"
    finally:
        await app.close()

    sup = await asyncpg.connect(rls_db.superuser_dsn)
    try:
        row = await sup.fetchrow(
            "SELECT job_id FROM audit_logs WHERE id = $1", audit_id
        )
        assert row is not None, "audit row must survive the job delete"
        assert row["job_id"] is None, "FK ON DELETE SET NULL must have nulled job_id"
    finally:
        await sup.close()
