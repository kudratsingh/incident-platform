"""Postgres row-level security enforcement test.

Boots a real Postgres in a container, runs the full Alembic migration
chain (including the RLS policy migration), then proves:

  1. With `app.tenant_id` set to tenant A, queries see only A's rows.
  2. With it set to B, only B's rows are visible.
  3. With it unset, *all* rows are visible — this is the bootstrap escape
     hatch the policy was deliberately written with (workers and
     migrations don't carry a tenant context).

The third assertion is intentional: it documents the trade-off the
migration was written under. If a future change tightens the policy
(removes the IS NULL escape), this test will fail loudly and force the
author to migrate workers + relays to set the variable per transaction.

Skipped automatically when Docker / testcontainers isn't available so the
rest of the suite still runs.
"""

import os
import uuid
from typing import Any

import pytest

try:
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    _HAS_TC = True
except Exception:  # pragma: no cover
    _HAS_TC = False

pytestmark = pytest.mark.skipif(
    not _HAS_TC or not os.environ.get("RUN_RLS_TEST"),
    reason="set RUN_RLS_TEST=1 and install Docker + testcontainers[postgres] to run",
)


@pytest.fixture(scope="module")
def pg() -> Any:
    with PostgresContainer("postgres:16-alpine") as container:
        yield container


def _run_migrations(sync_url: str) -> None:
    """Run alembic upgrade head against the container."""
    import subprocess

    env = os.environ.copy()
    env["DATABASE_URL"] = sync_url
    env["RUN_ALEMBIC_SYNC"] = "1"
    # alembic.ini lives at repo root; backend/ is cwd
    subprocess.check_call(
        ["alembic", "upgrade", "head"],
        env=env,
    )


async def test_rls_isolates_tenants(pg: Any) -> None:
    """Two tenants insert jobs. With app.tenant_id set, each sees only its own.

    Connects as a fresh non-superuser role so the policy actually applies.
    """
    import asyncpg

    superuser_dsn = pg.get_connection_url().replace("postgresql+psycopg2", "postgresql")
    sync_url = pg.get_connection_url()
    _run_migrations(sync_url)

    sup = await asyncpg.connect(superuser_dsn)
    try:
        # Create a non-superuser role and grant it read/write on jobs.
        # Plain users don't bypass RLS, so the policy enforces.
        await sup.execute("CREATE ROLE app_role LOGIN PASSWORD 'app_pw'")
        await sup.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON jobs TO app_role")
        await sup.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON tenants TO app_role")
        await sup.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON users TO app_role")

        tenant_a = uuid.uuid4()
        tenant_b = uuid.uuid4()
        await sup.execute(
            "INSERT INTO tenants (id, slug, name, is_active) "
            "VALUES ($1, $2, $3, true), ($4, $5, $6, true)",
            tenant_a, "tenant-a", "A", tenant_b, "tenant-b", "B",
        )
        user_a = uuid.uuid4()
        user_b = uuid.uuid4()
        await sup.execute(
            "INSERT INTO users (id, tenant_id, email, hashed_password, role, is_active) "
            "VALUES ($1, $2, $3, 'x', 'user', true), ($4, $5, $6, 'x', 'user', true)",
            user_a, tenant_a, "a@a.test", user_b, tenant_b, "b@b.test",
        )
        await sup.execute(
            "INSERT INTO jobs (id, tenant_id, user_id, type, status, priority, payload) "
            "VALUES ($1, $2, $3, 'csv_upload', 'pending', 5, '{}'::jsonb), "
            "       ($4, $5, $6, 'csv_upload', 'pending', 5, '{}'::jsonb)",
            uuid.uuid4(), tenant_a, user_a,
            uuid.uuid4(), tenant_b, user_b,
        )
    finally:
        await sup.close()

    # Connect as the non-superuser app role — RLS now applies.
    app_dsn = superuser_dsn.replace(
        "postgres:postgres@", "app_role:app_pw@"
    )
    app = await asyncpg.connect(app_dsn)
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
