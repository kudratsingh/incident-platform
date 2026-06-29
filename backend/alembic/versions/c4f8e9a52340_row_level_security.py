"""row-level security on every tenant-scoped table

Revision ID: c4f8e9a52340
Revises: b3d8e7a52116
Create Date: 2026-06-29 13:00:00.000000

Postgres-only defence-in-depth. With these policies in place, a query that
forgets to filter by tenant_id at the application layer still returns no
rows when run inside a request that has set `app.tenant_id` to a different
tenant.

Production deployment note
==========================
Policies apply only to roles that don't already bypass RLS. The app
process must connect as a non-owner role; otherwise SQLAlchemy operates
as the table owner and the policy is silently ignored. Two roles:

  incident_owner   table owner; used for migrations and the worker process
                   (`bypassrls` implicit via ownership). Workers handle
                   multi-tenant traffic and shouldn't be locked into a
                   single tenant per session.

  incident_app     non-owner; SELECT/INSERT/UPDATE/DELETE granted on
                   tenant-scoped tables. Used by the FastAPI request
                   process. RLS applies here.

For the integration test (test_rls_enforcement) we set this up explicitly.
The bootstrap migration is intentionally permissive when `app.tenant_id`
is unset (empty string or NULL) so workers and migrations keep working
without per-statement tenant setup.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c4f8e9a52340"
down_revision: str | Sequence[str] | None = "b3d8e7a52116"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLES = [
    "jobs",
    "audit_logs",
    "outbox_events",
    "job_events",
    "sagas",
    "job_triages",
]


def upgrade() -> None:
    # Skip on non-Postgres dialects (SQLite test DB ignores this).
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
              USING (
                current_setting('app.tenant_id', true) IS NULL
                OR current_setting('app.tenant_id', true) = ''
                OR tenant_id = current_setting('app.tenant_id', true)::uuid
              )
              WITH CHECK (
                current_setting('app.tenant_id', true) IS NULL
                OR current_setting('app.tenant_id', true) = ''
                OR tenant_id = current_setting('app.tenant_id', true)::uuid
              )
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
