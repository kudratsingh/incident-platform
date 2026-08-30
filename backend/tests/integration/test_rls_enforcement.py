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
  7. audit_logs is immutable at the DB layer: UPDATE/DELETE as the
     runtime role raise insufficient_privilege — a loud error, because
     migration b8e4a1c92f35 revokes those grants from incident_app
     (F1-07). INSERT with a matching tenant still succeeds.
  8. Deleting a job still nulls audit_logs.job_id via the FK's ON DELETE
     SET NULL: referential actions execute with the referencing table
     owner's privileges and bypass RLS, so neither the grant revoke nor
     the restrictive deny policies break scripts/reset_eval_state.py.
  9. The migration-vs-runtime split is real: CREATE TABLE / ALTER TABLE
     as incident_app raise insufficient_privilege, while the grants do
     cover alembic_version (assert_migrations_current reads it at boot).
 10. The boot posture probe (app.core.rls_check.assert_rls_posture)
     passes on a live incident_app engine and raises on a superuser
     engine under production settings.
 11. An audit INSERT for a FOREIGN tenant is refused by the WITH CHECK
     and accepted once `app.tenant_id` is retargeted at that tenant —
     the constraint the operator-audit writes in app/api/admin.py are
     built around (F1-08).

Since WO-P2-03 the non-superuser sessions here connect as the actual
production runtime role: `incident_app`, created by the migration chain
itself and given its password by `python -m app.core.db_bootstrap`
(exactly what scripts/entrypoint.sh and the compose migrate one-shot
run) — not a hand-rolled test role.

The third assertion is intentional: it documents the trade-off the
migration was written under. If a future change tightens the policy
(removes the IS NULL escape), this test will fail loudly and force the
author to migrate workers + relays to set the variable per transaction.

Skipped automatically when Docker / testcontainers isn't available so the
rest of the suite still runs.
"""

import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from app.core.rls_check import tenant_scoped_tables

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

# Every tenant-scoped table under RLS, from the same derivation the boot
# probe uses (WO-R2-26) rather than a third hand-maintained copy: the ORM
# metadata, minus the `users` bootstrap exemption. A new tenant-scoped
# table joins this list, the unit coverage gate and the runtime probe at
# once, the moment its model exists.
ALL_TENANT_RLS_TABLES = sorted(tenant_scoped_tables())


@pytest.fixture(scope="module")
def pg() -> Any:
    # driver="asyncpg" so get_connection_url() emits postgresql+asyncpg://
    # (asyncpg is a main dependency; psycopg2 is not installed).
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


def _alembic(database_url: str, *args: str) -> None:
    """Run an alembic command against the container.

    Uses the current interpreter and an absolute -c path so the call
    works regardless of cwd and PATH; env.py routes on the URL's dialect
    (postgresql+asyncpg here, so the async engine path).

    ALEMBIC_DATABASE_URL is popped, not just overridden: `env.py::_get_url`
    prefers it over DATABASE_URL (ADR 0015's two-URL scheme), so an
    inherited value from the developer's shell or a CI job env would
    silently redirect this fixture's destructive `upgrade / downgrade -1 /
    upgrade` cycle at whatever database that variable names instead of the
    throwaway container.
    """
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env.pop("ALEMBIC_DATABASE_URL", None)
    subprocess.check_call(
        [sys.executable, "-m", "alembic", "-c", str(REPO_ROOT / "alembic.ini"), *args],
        env=env,
        cwd=REPO_ROOT,
    )


def _run_db_bootstrap(database_url: str, password: str) -> None:
    """Invoke the real boot-time password sync, exactly as the entrypoint
    and the compose migrate one-shot do: `python -m app.core.db_bootstrap`
    with the owner URL and INCIDENT_APP_DB_PASSWORD in the environment."""
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


@dataclass(frozen=True)
class RlsDb:
    superuser_dsn: str
    app_dsn: str
    app_async_url: str
    superuser_async_url: str


@pytest.fixture(scope="module")
def rls_db(pg: Any) -> RlsDb:
    """Migrated database with the production `incident_app` role, set up
    once per module.

    The migration chain itself creates `incident_app` (b8e4a1c92f35) and
    `python -m app.core.db_bootstrap` gives it a password — the same two
    steps scripts/entrypoint.sh runs at every boot — so the non-superuser
    sessions in this module exercise the exact role production connects
    as after the phase-2 flip. A `downgrade -1` / re-`upgrade head`
    round-trip proves the role migration reverses cleanly. The superuser
    DSN is for fixtures/verification only — superusers bypass RLS even
    under FORCE.
    """
    host = pg.get_container_host_ip()
    port = pg.get_exposed_port(5432)
    superuser_dsn = f"postgresql://{pg.username}:{pg.password}@{host}:{port}/{pg.dbname}"
    app_dsn = f"postgresql://incident_app:app_pw@{host}:{port}/{pg.dbname}"

    async_url = pg.get_connection_url()
    _alembic(async_url, "upgrade", "head")
    # Named revision, not a relative "-1": the point is to reverse the
    # role+grants migration specifically, and a relative step silently
    # retargets itself at whatever migration landed on head most
    # recently. Downgrading TO b8e4a1c92f35's parent reverses it (and
    # anything stacked on top) whatever the head of the day is.
    _alembic(async_url, "downgrade", "b8e4a1c92f35")
    _alembic(async_url, "downgrade", "-1")  # drops role + grants (b8e4a1c92f35)
    _alembic(async_url, "upgrade", "head")  # recreates them, and the rest
    _run_db_bootstrap(async_url, "app_pw")

    return RlsDb(
        superuser_dsn=superuser_dsn,
        app_dsn=app_dsn,
        app_async_url=f"postgresql+asyncpg://incident_app:app_pw@{host}:{port}/{pg.dbname}",
        superuser_async_url=async_url,
    )


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

    Connects as the production non-owner role (incident_app) so the
    policy applies exactly as it does after the phase-2 flip.
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


async def test_audit_logs_update_delete_raise_insufficient_privilege(rls_db: RlsDb) -> None:
    """audit_logs tampering is a loud error for the runtime role (F1-07).

    Migration b8e4a1c92f35 revokes UPDATE/DELETE on audit_logs from
    incident_app, so tampering fails at the grant layer with
    insufficient_privilege — an error, no longer the silent 'UPDATE 0'
    no-op that the restrictive deny policies alone produced for a
    DML-granted role (that pre-P2-03 assertion lived in this test; the
    deny policies still back-stop any owner-connected session).
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
        # Scope to the row's own tenant: the permissive tenant_isolation
        # policy would admit the row, so what refuses the write is
        # precisely the revoked grant.
        async with app.transaction():
            await app.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await app.execute(
                    "UPDATE audit_logs SET action = 'tampered' WHERE id = $1", audit_id
                )
        async with app.transaction():
            await app.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await app.execute("DELETE FROM audit_logs WHERE id = $1", audit_id)
    finally:
        await app.close()

    sup = await asyncpg.connect(rls_db.superuser_dsn)
    try:
        row = await sup.fetchrow("SELECT action FROM audit_logs WHERE id = $1", audit_id)
        assert row is not None and row["action"] == "job.created"
    finally:
        await sup.close()


async def test_audit_insert_with_matching_tenant_succeeds(rls_db: RlsDb) -> None:
    """Append stays open: the runtime role can still INSERT audit rows.

    The revoke covers UPDATE/DELETE only — audit_logs is append-only,
    not read-only, for incident_app.
    """
    import asyncpg

    sup = await asyncpg.connect(rls_db.superuser_dsn)
    try:
        tenant = await _create_tenant(sup, "audit-append")
    finally:
        await sup.close()

    app = await asyncpg.connect(rls_db.app_dsn)
    try:
        async with app.transaction():
            await app.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
            tag = await app.execute(
                "INSERT INTO audit_logs (id, tenant_id, action) VALUES ($1, $2, 'job.created')",
                uuid.uuid4(), tenant,
            )
            assert tag == "INSERT 0 1", f"audit INSERT must succeed, got {tag!r}"
    finally:
        await app.close()


async def test_foreign_tenant_audit_insert_needs_retargeted_setting(
    rls_db: RlsDb,
) -> None:
    """The WITH CHECK that shapes the operator-audit writes in admin.py.

    `POST /admin/tenants` and `PATCH /admin/tenants/{id}` write an audit
    row belonging to the tenant being acted on, while the request's
    `app.tenant_id` is the platform admin's OWN tenant. That INSERT is
    refused (F1-08), which is why `app/api/admin.py::_set_rls_tenant`
    retargets the setting at the subject tenant for the write — the same
    `set_config` move `resolve_admin_tenant` makes for cross-tenant reads,
    with no policy relaxed. Both halves are asserted here.
    """
    import asyncpg

    sup = await asyncpg.connect(rls_db.superuser_dsn)
    try:
        admin_home = await _create_tenant(sup, "audit-xt-home")
        subject = await _create_tenant(sup, "audit-xt-subject")
    finally:
        await sup.close()

    app = await asyncpg.connect(rls_db.app_dsn)
    try:
        # Naive version: session scoped to the admin's home tenant.
        async with app.transaction():
            await app.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(admin_home)
            )
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await app.execute(
                    "INSERT INTO audit_logs (id, tenant_id, action) "
                    "VALUES ($1, $2, 'tenant.created')",
                    uuid.uuid4(), subject,
                )
        # With the setting retargeted at the subject tenant, it lands.
        async with app.transaction():
            await app.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(subject)
            )
            tag = await app.execute(
                "INSERT INTO audit_logs (id, tenant_id, action) "
                "VALUES ($1, $2, 'tenant.created')",
                uuid.uuid4(), subject,
            )
            assert tag == "INSERT 0 1", f"retargeted INSERT must succeed, got {tag!r}"
    finally:
        await app.close()


async def test_ddl_denied_for_incident_app(rls_db: RlsDb) -> None:
    """The migration-vs-runtime split is real: no DDL for incident_app.

    CREATE TABLE needs CREATE on the schema (only USAGE is granted);
    ALTER TABLE needs table ownership (the migration role owns
    everything). Both must fail with insufficient_privilege — DDL,
    TRUNCATE and DROP POLICY power stays off the network-facing process.
    """
    import asyncpg

    app = await asyncpg.connect(rls_db.app_dsn)
    try:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await app.execute("CREATE TABLE rls_smoke_probe (id int)")
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await app.execute("ALTER TABLE jobs ADD COLUMN rls_smoke_probe int")
    finally:
        await app.close()


async def test_incident_app_can_read_alembic_version(rls_db: RlsDb) -> None:
    """GRANT ... ON ALL TABLES includes alembic_version — required: both
    lifespans run assert_migrations_current as incident_app at boot."""
    import asyncpg

    app = await asyncpg.connect(rls_db.app_dsn)
    try:
        version = await app.fetchval("SELECT version_num FROM alembic_version")
        assert version, "incident_app must be able to read alembic_version"
    finally:
        await app.close()


async def test_rls_posture_probe_against_live_engines(rls_db: RlsDb) -> None:
    """assert_rls_posture on real engines: the probe SQL itself.

    As incident_app (non-superuser, non-owner) the posture is healthy —
    no raise even under production settings. As the container superuser
    RLS is bypassed wholesale, so production settings must raise. This is
    the boot-time negative probe F1-01 asked for: it would have flagged
    the original prod posture (owner, unforced) on the first deploy.
    """
    from app.config import Settings
    from app.core.rls_check import assert_rls_posture
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    prod_settings = Settings(_env_file=None, environment="production", secret_key="x" * 48)

    app_engine = create_async_engine(rls_db.app_async_url)
    try:
        await assert_rls_posture(
            async_sessionmaker(app_engine, expire_on_commit=False), prod_settings
        )  # no raise
    finally:
        await app_engine.dispose()

    sup_engine = create_async_engine(rls_db.superuser_async_url)
    try:
        factory = async_sessionmaker(sup_engine, expire_on_commit=False)
        with pytest.raises(RuntimeError, match="row-level security"):
            await assert_rls_posture(factory, prod_settings)
    finally:
        await sup_engine.dispose()


async def test_job_delete_still_nulls_audit_fk_via_ri_bypass(rls_db: RlsDb) -> None:
    """Deleting a job must still SET NULL audit_logs.job_id.

    Referential actions execute with the *referencing table owner's*
    privileges and bypass row security, so neither the UPDATE revoke on
    audit_logs nor the restrictive deny policies block the FK's ON
    DELETE SET NULL — this is what keeps scripts/reset_eval_state.py
    (which deletes jobs and users) working as incident_app. Do not "fix"
    a failing cascade by re-granting UPDATE.
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


# ---------------------------------------------------------------------------
# R2-26 — the boot probe must catch RLS being OFF, not just unFORCEd
# ---------------------------------------------------------------------------


async def _probe_as_app(rls_db: RlsDb, environment: str) -> None:
    """Run the boot probe over the production (non-owner) role."""
    from app.config import Settings
    from app.core.rls_check import assert_rls_posture
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    settings = Settings(
        _env_file=None, environment=environment, secret_key="x" * 48
    )
    engine = create_async_engine(rls_db.app_async_url)
    try:
        await assert_rls_posture(
            async_sessionmaker(engine, expire_on_commit=False), settings
        )
    finally:
        await engine.dispose()


async def test_probe_catches_rls_switched_off_on_one_table(
    rls_db: RlsDb, caplog: Any
) -> None:
    """A table with row-level security switched off entirely used to pass
    as 'rls posture ok'.

    The probe never selected `pg_class.relrowsecurity`, so it could only
    see the *owner exemption* (FORCE off). For the non-owner production
    role its inert calculation short-circuited to False no matter what,
    which means the one runtime tripwire on the security boundary was
    blind to the most direct way of disabling it. `DISABLE ROW LEVEL
    SECURITY` on any tenant table and every query against it is
    unconstrained.
    """
    import asyncpg

    sup = await asyncpg.connect(rls_db.superuser_dsn)
    try:
        await sup.execute("ALTER TABLE job_events DISABLE ROW LEVEL SECURITY")

        with pytest.raises(RuntimeError, match="row-level security"):
            await _probe_as_app(rls_db, "production")

        # Outside production the probe must still say so, loudly, and boot.
        caplog.clear()
        with caplog.at_level("ERROR"):
            await _probe_as_app(rls_db, "development")
        assert any(
            "job_events" in record.getMessage() for record in caplog.records
        ), "the posture failure named no table"
    finally:
        await sup.execute("ALTER TABLE job_events ENABLE ROW LEVEL SECURITY")
        await sup.execute("ALTER TABLE job_events FORCE ROW LEVEL SECURITY")
        await sup.close()

    # Restored: healthy again.
    await _probe_as_app(rls_db, "production")


async def test_probe_catches_a_dropped_tenant_isolation_policy(
    rls_db: RlsDb,
) -> None:
    """RLS ENABLEd + FORCEd with no policy is not a safe posture — it is
    a different one. Postgres denies by default when no policy matches,
    so a dropped `tenant_isolation` does not leak rows; it silently
    breaks the table instead, and either way the posture the probe
    claims to verify is gone. It must not report ok.
    """
    import asyncpg

    sup = await asyncpg.connect(rls_db.superuser_dsn)
    try:
        await sup.execute("DROP POLICY tenant_isolation ON sagas")

        with pytest.raises(RuntimeError, match="row-level security"):
            await _probe_as_app(rls_db, "production")
    finally:
        await sup.execute(
            "CREATE POLICY tenant_isolation ON sagas "
            "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)"
        )
        await sup.close()

    await _probe_as_app(rls_db, "production")


async def test_a_cleared_tenant_setting_turns_digest_isolation_off(
    rls_db: RlsDb,
) -> None:
    """WO-R2-127, and why it is a security finding rather than a tidy.

    `app.tenant_id` is set with `set_config(..., true)` — **transaction-local**.
    The admin digest route reads the window, ends that transaction so the
    Anthropic round-trip holds no connection, and then INSERTs the result in a
    new one. The setting does not carry over.

    The intuition is that the unscoped INSERT is then rejected. It is not.
    Every `tenant_isolation` policy here opens with

        current_setting('app.tenant_id', true) IS NULL OR ... = '' OR ...

    — the ADR 0003 bootstrap hatch that lets authentication read `users` before
    any tenant is known. An unset setting does not fail closed, it **fails
    open**: the policy is satisfied unconditionally, the statement runs with no
    tenant isolation whatsoever, and nothing errors or logs. A write that
    should have been scoped simply is not, which is the failure this work order
    removes — RLS is the backstop for a forgotten filter, and here the backstop
    was silently off for the one statement that had already cost real money.
    """
    import asyncpg

    sup = await asyncpg.connect(rls_db.superuser_dsn)
    try:
        home = await _create_tenant(sup, "digest-rls-home")
        foreign = await _create_tenant(sup, "digest-rls-foreign")
    finally:
        await sup.close()

    insert = (
        "INSERT INTO incident_summaries "
        "(id, tenant_id, window_start, window_end, summary, model_used) "
        "VALUES ($1, $2, now(), now(), 'digest', 'claude')"
    )

    app = await asyncpg.connect(rls_db.app_dsn)
    try:
        # A connection that has never been scoped — the setting is unset, and
        # the policy's first branch admits everything.
        async with app.transaction():
            assert (
                await app.fetchval("SELECT current_setting('app.tenant_id', true)")
            ) is None
            tag = await app.execute(insert, uuid.uuid4(), foreign)
            assert tag == "INSERT 0 1", (
                "an unscoped session must be shown to fail OPEN — if this ever "
                "starts raising, the bootstrap branch changed and this finding "
                "needs rewriting, not deleting"
            )
            assert await app.fetchval(
                "SELECT count(*) FROM incident_summaries WHERE tenant_id = $1",
                foreign,
            ) == 1, "an unscoped session reads every tenant's digests too"

        # Scope it, the way the read phase does.
        async with app.transaction():
            await app.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(home)
            )
            assert await app.fetchval(
                "SELECT current_setting('app.tenant_id', true)"
            ) == str(home)

        # The write phase's transaction: the value is gone. On a pooled
        # connection it resets to the empty string rather than to unset, which
        # is its own hazard — `''::uuid` is not a cast Postgres will make, so
        # the third branch of the policy can raise outright instead of failing
        # open. Either way it is not the caller's tenant.
        async with app.transaction():
            assert await app.fetchval(
                "SELECT current_setting('app.tenant_id', true)"
            ) != str(home), "the GUC must not survive the transaction that set it"

        # Re-established on the write session — the whole of the fix. A
        # cross-tenant row is refused, the caller's own row lands.
        async with app.transaction():
            await app.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(home)
            )
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await app.execute(insert, uuid.uuid4(), foreign)

        async with app.transaction():
            await app.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(home)
            )
            tag = await app.execute(insert, uuid.uuid4(), home)
            assert tag == "INSERT 0 1", f"own-tenant INSERT must succeed, got {tag!r}"
    finally:
        await app.close()
