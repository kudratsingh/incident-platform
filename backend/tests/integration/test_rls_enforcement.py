"""Postgres row-level security enforcement test.

Boots a real Postgres, runs the whole Alembic chain, then proves per-tenant visibility; an
unscoped session refused, not admitted (WO-R2-129 / ADR 0026, where cross-tenant work
declares `app.tenant_scope = 'platform'`); ENABLE + FORCE everywhere (F1-01, F1-05); the
`deploy_markers` NULL variant; `audit_logs` immutability (F1-07); F1-08; the boot probe.
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

# Every tenant-scoped table under RLS, from the derivation the boot probe uses
# (WO-R2-26): the ORM metadata minus the `users` bootstrap exemption.
ALL_TENANT_RLS_TABLES = sorted(tenant_scoped_tables())

# The strict `tenant_isolation` predicate as shipped by e2a9c4f70b31 (ADR 0026), so a
# test that rebuilds a policy restores the real one.
_STRICT_MATCH = (
    "current_setting('app.tenant_scope', true) = 'platform'"
    " OR tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
)


@pytest.fixture(scope="module")
def pg() -> Any:
    # driver="asyncpg" so get_connection_url() emits postgresql+asyncpg://
    # (asyncpg is a main dependency; psycopg2 is not installed).
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


def _alembic(database_url: str, *args: str) -> None:
    """Run an alembic command against the container. ALEMBIC_DATABASE_URL is popped,
    not overridden: `env.py::_get_url` prefers it (ADR 0015), so an inherited value
    would point this fixture's destructive upgrade/downgrade cycle elsewhere."""
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env.pop("ALEMBIC_DATABASE_URL", None)
    subprocess.check_call(
        [sys.executable, "-m", "alembic", "-c", str(REPO_ROOT / "alembic.ini"), *args],
        env=env,
        cwd=REPO_ROOT,
    )


def _run_db_bootstrap(database_url: str, password: str) -> None:
    """The real boot-time password sync, via `python -m app.core.db_bootstrap`."""
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
    """Migrated database with the production `incident_app` role, once per module:
    b8e4a1c92f35 creates it, db_bootstrap gives it a password, and the round-trip
    proves that migration reverses. The superuser DSN is for fixtures only."""
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
    """Two tenants insert jobs; each scoped session sees only its own, as the
    non-owner `incident_app`."""
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
        # retry_count / max_attempts are NOT NULL without server defaults
        # (the defaults are ORM-side), so the raw INSERT must supply them.
        await sup.execute(
            "INSERT INTO jobs (id, tenant_id, user_id, type, status, priority, "
            "                  retry_count, max_attempts, payload) "
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

        # Unset — refused, not admitted (WO-R2-129). This assertion is the
        # inverse of the one that stood here: the bootstrap escape hatch used
        # to make an unscoped read return every tenant's rows.
        async with app.transaction():
            rows = await app.fetch("SELECT tenant_id FROM jobs")
            assert rows == [], "an unscoped read must see nothing"

        # Cross-tenant work declares itself instead of being admitted for
        # having forgotten — this is what the worker loops, the migration
        # runner and the seed/reset scripts now do (ADR 0026).
        async with app.transaction():
            await app.execute(
                "SELECT set_config('app.tenant_scope', 'platform', true)"
            )
            rows = await app.fetch("SELECT tenant_id FROM jobs")
            assert len(rows) == 2, "declared platform scope must span tenants"
    finally:
        await app.close()


async def test_rls_enabled_and_forced_on_all_tenant_tables(rls_db: RlsDb) -> None:
    """RLS ENABLEd and FORCEd everywhere (F1-01): without FORCE the owner is
    exempt, and production is the RDS master."""
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
    """`deploy_markers` keeps NULL-tenant rows visible, or `get_deploy_history`
    degrades to its env-var fallback. Other tenants' rows stay hidden."""
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
    """audit_logs tampering is a loud error (F1-07): b8e4a1c92f35 revokes
    UPDATE/DELETE, so it fails at the grant layer, not as a silent `UPDATE 0`."""
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
    """Append stays open: the revoke is UPDATE/DELETE only."""
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
    """The WITH CHECK behind admin.py's operator-audit writes: an audit row for the
    subject tenant is refused while `app.tenant_id` is the admin's own (F1-08), which
    is why `_set_rls_tenant` retargets the setting instead of relaxing a policy."""
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
    """No DDL for incident_app: CREATE needs schema CREATE (only USAGE is granted)
    and ALTER needs ownership, so DROP POLICY power stays off the facing process."""
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
    """alembic_version must be readable: both lifespans check it at boot."""
    import asyncpg

    app = await asyncpg.connect(rls_db.app_dsn)
    try:
        version = await app.fetchval("SELECT version_num FROM alembic_version")
        assert version, "incident_app must be able to read alembic_version"
    finally:
        await app.close()


async def test_rls_posture_probe_against_live_engines(rls_db: RlsDb) -> None:
    """assert_rls_posture on real engines: healthy as incident_app, raising as the
    superuser under production settings — the negative probe F1-01 asked for."""
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
    """Deleting a job must still SET NULL audit_logs.job_id: referential actions run
    with the referencing owner's privileges and bypass row security. Do not "fix" a
    failure here by re-granting UPDATE."""
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
            "                  retry_count, max_attempts, payload) "
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


# R2-26 — the boot probe must catch RLS being OFF, not just unFORCEd


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
    """A table with RLS switched off entirely used to pass as ok: the probe never
    selected `pg_class.relrowsecurity`, so it saw only the owner exemption and was
    blind to the most direct way of disabling the boundary."""
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
    """ENABLEd + FORCEd with no policy is a different posture, not a safe one, and
    the probe must not report ok."""
    import asyncpg

    sup = await asyncpg.connect(rls_db.superuser_dsn)
    try:
        await sup.execute("DROP POLICY tenant_isolation ON sagas")

        with pytest.raises(RuntimeError, match="row-level security"):
            await _probe_as_app(rls_db, "production")
    finally:
        # Restore the policy the migration creates, WITH CHECK and all: a USING-only
        # rebuild left every later test running against an unconstrained `sagas`.
        await sup.execute(
            "CREATE POLICY tenant_isolation ON sagas"
            "  USING (" + _STRICT_MATCH + ")"
            "  WITH CHECK (" + _STRICT_MATCH + ")"
        )
        await sup.close()

    await _probe_as_app(rls_db, "production")


async def test_a_cleared_tenant_setting_is_refused_not_admitted(
    rls_db: RlsDb,
) -> None:
    """WO-R2-129, the inversion of WO-R2-127: `app.tenant_id` is transaction-local, so
    the digest route's write phase is unscoped. The ADR 0003 bootstrap hatch made that
    fail open; with it gone the write trips WITH CHECK and the read returns nothing.
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
        # A connection that has never been scoped. The write is refused and
        # the read is empty — the exact pair that used to succeed.
        async with app.transaction():
            assert (
                await app.fetchval("SELECT current_setting('app.tenant_id', true)")
            ) is None
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await app.execute(insert, uuid.uuid4(), foreign)

        async with app.transaction():
            assert await app.fetchval(
                "SELECT count(*) FROM incident_summaries"
            ) == 0, "an unscoped session must read no tenant's digests"

        # The pooled-connection variant of the same hazard: the GUC resets to
        # the empty string rather than to unset. That used to reach
        # `''::uuid` and raise invalid_text_representation — a different
        # error for the same mistake. `nullif(..., '')` folds it into the
        # same clean refusal. Note the refusal aborts its transaction, so
        # the follow-up count gets a fresh one.
        async with app.transaction():
            await app.execute("SELECT set_config('app.tenant_id', '', true)")
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await app.execute(insert, uuid.uuid4(), foreign)

        async with app.transaction():
            assert await app.fetchval(
                "SELECT count(*) FROM incident_summaries"
            ) == 0

        # Scope it, the way the read phase does.
        async with app.transaction():
            await app.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(home)
            )
            assert await app.fetchval(
                "SELECT current_setting('app.tenant_id', true)"
            ) == str(home)

        # The write phase's transaction: the value is gone.
        async with app.transaction():
            assert await app.fetchval(
                "SELECT current_setting('app.tenant_id', true)"
            ) != str(home), "the GUC must not survive the transaction that set it"

        # Re-established on the write session — a cross-tenant row is still
        # refused, the caller's own row lands.
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


async def test_unscoped_writes_are_refused_on_every_tenant_table(
    rls_db: RlsDb,
) -> None:
    """Never specific to digests (WO-R2-129): the bootstrap branch was in all eleven
    policies, so ask the server what each one ended up with."""
    import asyncpg

    sup = await asyncpg.connect(rls_db.superuser_dsn)
    try:
        rows = await sup.fetch(
            "SELECT tablename, qual, with_check FROM pg_policies "
            "WHERE policyname = 'tenant_isolation' ORDER BY tablename"
        )
    finally:
        await sup.close()

    assert {r["tablename"] for r in rows} == set(ALL_TENANT_RLS_TABLES), (
        "every tenant-scoped table must carry a tenant_isolation policy"
    )

    for row in rows:
        for clause in (row["qual"], row["with_check"]):
            assert clause is not None, f"{row['tablename']}: missing clause"
            normalised = " ".join(clause.split())
            assert "IS NULL" not in normalised.replace(
                "tenant_id IS NULL", ""
            ), (
                f"{row['tablename']}: the bootstrap branch is back — {normalised}"
            )
            assert "= ''::text" not in normalised, (
                f"{row['tablename']}: empty-string escape is back — {normalised}"
            )


async def test_platform_scope_is_what_lets_the_worker_loops_work(
    rls_db: RlsDb,
) -> None:
    """The declared-scope half: the loops are mixed-tenant by construction, and the
    declaration is transaction-local so it cannot leak onto the next request."""
    import asyncpg

    sup = await asyncpg.connect(rls_db.superuser_dsn)
    try:
        tenant = await _create_tenant(sup, "platform-scope-probe")
    finally:
        await sup.close()

    app = await asyncpg.connect(rls_db.app_dsn)
    try:
        summary_id = uuid.uuid4()
        async with app.transaction():
            await app.execute(
                "SELECT set_config('app.tenant_scope', 'platform', true)"
            )
            tag = await app.execute(
                "INSERT INTO incident_summaries (id, tenant_id, window_start, "
                "window_end, summary, model_used) "
                "VALUES ($1, $2, now(), now(), 'digest', 'claude')",
                summary_id,
                tenant,
            )
            assert tag == "INSERT 0 1"

        # New transaction, nothing declared: the scope is gone with it.
        async with app.transaction():
            assert await app.fetchval(
                "SELECT current_setting('app.tenant_scope', true)"
            ) != "platform", "platform scope must not outlive its transaction"
            assert await app.fetchval(
                "SELECT count(*) FROM incident_summaries WHERE id = $1", summary_id
            ) == 0
    finally:
        await app.close()


async def test_service_accounts_preauth_read_survives_but_writes_do_not(
    rls_db: RlsDb,
) -> None:
    """The one genuine non-`users` bootstrap consumer (ADR 0026): `verify_token` reads
    `service_accounts` before `_apply_tenant_context` can name the tenant, so the
    unscoped SELECT has to work — but it is restored `FOR SELECT` only."""
    import asyncpg

    sup = await asyncpg.connect(rls_db.superuser_dsn)
    try:
        tenant = await _create_tenant(sup, "sa-bootstrap")
        sa_id = uuid.uuid4()
        await sup.execute(
            "INSERT INTO service_accounts (id, tenant_id, name, scopes, "
            "is_active) VALUES ($1, $2, 'probe', '[]'::jsonb, true)",
            sa_id,
            tenant,
        )
    finally:
        await sup.close()

    app = await asyncpg.connect(rls_db.app_dsn)
    try:
        # The pre-auth lookup: unscoped, and it must find the row.
        async with app.transaction():
            assert (
                await app.fetchval("SELECT current_setting('app.tenant_id', true)")
            ) is None
            found = await app.fetchval(
                "SELECT tenant_id FROM service_accounts WHERE id = $1", sa_id
            )
            assert found == tenant, "the pre-auth service-account read must work"

        # ...but the bootstrap policy is SELECT-only.
        async with app.transaction():
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await app.execute(
                    "INSERT INTO service_accounts (id, tenant_id, name, "
                    "scopes, is_active) "
                    "VALUES ($1, $2, 'forged', '[]'::jsonb, true)",
                    uuid.uuid4(),
                    tenant,
                )

        # And it buys nothing once a tenant IS named: a scoped session sees
        # only its own rows, so the bootstrap read cannot be used as a
        # cross-tenant window from inside an authenticated request.
        other = uuid.uuid4()
        async with app.transaction():
            await app.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(other)
            )
            assert await app.fetchval(
                "SELECT count(*) FROM service_accounts WHERE id = $1", sa_id
            ) == 0
    finally:
        await app.close()


async def test_unscoped_deploy_marker_write_is_limited_to_platform_rows(
    rls_db: RlsDb,
) -> None:
    """`deploy_markers` keeps `OR tenant_id IS NULL` (ADR 0015); what WO-R2-129
    changes is reach — an unscoped session can no longer forge a named tenant's."""
    import asyncpg

    sup = await asyncpg.connect(rls_db.superuser_dsn)
    try:
        tenant = await _create_tenant(sup, "deploy-marker-scope")
    finally:
        await sup.close()

    app = await asyncpg.connect(rls_db.app_dsn)
    try:
        async with app.transaction():
            tag = await app.execute(
                "INSERT INTO deploy_markers (id, tenant_id, version, "
                "environment, deployed_at) "
                "VALUES ($1, NULL, 'v1', 'test', now())",
                uuid.uuid4(),
            )
            assert tag == "INSERT 0 1", "platform-wide markers stay writable"

        async with app.transaction():
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await app.execute(
                    "INSERT INTO deploy_markers (id, tenant_id, version, "
                    "environment, deployed_at) "
                    "VALUES ($1, $2, 'v1', 'test', now())",
                    uuid.uuid4(),
                    tenant,
                )
    finally:
        await app.close()


async def test_the_real_platform_session_factory_declares_the_scope(
    rls_db: RlsDb,
) -> None:
    """Exercise `platform_session_factory` itself, not a hand-written GUC: everywhere
    else speaks raw asyncpg and SQLite makes the `after_begin` hook a no-op, so no
    tier would notice it stop emitting `set_config` and the relay fetching zero rows.
    """
    import asyncpg
    from app.core.tenant_scope import platform_session_factory
    from sqlalchemy import text as sa_text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    sup = await asyncpg.connect(rls_db.superuser_dsn)
    try:
        tenant = await _create_tenant(sup, "factory-probe")
        user_id = uuid.uuid4()
        await sup.execute(
            "INSERT INTO users (id, tenant_id, email, hashed_password, role, "
            "is_active) VALUES ($1, $2, 'factory@probe.test', 'x', 'user', true)",
            user_id,
            tenant,
        )
        await sup.execute(
            "INSERT INTO jobs (id, tenant_id, user_id, type, status, priority, "
            "retry_count, max_attempts, payload) "
            "VALUES ($1, $2, $3, 'csv_upload', 'pending', 5, 0, 3, '{}'::jsonb)",
            uuid.uuid4(),
            tenant,
            user_id,
        )
    finally:
        await sup.close()

    engine = create_async_engine(rls_db.app_async_url)
    try:
        # The factory the worker loops are handed.
        platform = platform_session_factory(engine)
        async with platform() as session:
            async with session.begin():
                assert (
                    await session.execute(
                        sa_text("SELECT current_setting('app.tenant_scope', true)")
                    )
                ).scalar() == "platform", "the after_begin hook did not fire"
                # Scoped to this test's own tenant: the module fixture is
                # shared, so earlier tests have left their own jobs behind.
                count = (
                    await session.execute(
                        sa_text(
                            "SELECT count(*) FROM jobs WHERE tenant_id = :t"
                        ),
                        {"t": tenant},
                    )
                ).scalar()
                assert count == 1, "declared platform scope must see the row"

        # The stock factory the request path uses — same engine, same pool,
        # no declaration, nothing visible.
        plain = async_sessionmaker(engine, expire_on_commit=False)
        async with plain() as session:
            async with session.begin():
                assert (
                    await session.execute(
                        sa_text("SELECT current_setting('app.tenant_scope', true)")
                    )
                ).scalar() != "platform", "platform scope leaked across factories"
                # No tenant named and no scope declared: the whole table is
                # invisible, not just this tenant's slice.
                count = (
                    await session.execute(sa_text("SELECT count(*) FROM jobs"))
                ).scalar()
                assert count == 0, "an undeclared session must see nothing"
    finally:
        await engine.dispose()
