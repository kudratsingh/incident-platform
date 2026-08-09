"""FORCE row-level security, cover the five missing tenant tables, make audit_logs immutable

Revision ID: a7e3d9c41f28
Revises: f4a7d2c85b19
Create Date: 2026-08-09 12:00:00.000000

Three defects, one layer (F1-01, F1-05, F1-07). See ADR 0015.

1. FORCE ROW LEVEL SECURITY on every covered table. c4f8e9a52340 only
   ENABLEd RLS, and Postgres exempts the table *owner* from policies
   unless FORCE is set. Production connects as the RDS master user
   (infra/variables.tf db_username=appuser -> rds.tf master ->
   secrets.tf DATABASE_URL), which owns every table — so RLS has been
   inert exactly where it matters. That master is an owner but NOT a
   superuser and has no BYPASSRLS, so FORCE alone is what flips the
   policies live for the production connection.

2. Five tenant tables created after c4f8e9a52340 shipped with no policy
   at all: incident_summaries, service_accounts, alerts,
   idempotency_records, deploy_markers. Each gets ENABLE ROW LEVEL
   SECURITY plus the exact tenant_isolation policy text from
   c4f8e9a52340, including the deliberate unset-tenant escape hatch
   (`IS NULL OR = ''`). The escape hatch is load-bearing: migrations
   (entrypoint alembic runs as the owner), worker loops, boot-time
   checks, and the pre-auth service_accounts lookup in
   get_current_principal all run with app.tenant_id unset. Do not
   tighten it (ADR 0003; ADR 0015).

   deploy_markers gets a variant with `OR tenant_id IS NULL` in both
   USING and WITH CHECK: tenant_id is nullable by design (deploys are
   platform-wide — see the model docstring in
   backend/app/models/deploy_marker.py), and the standard policy would
   hide every NULL row from tenant-scoped sessions, silently degrading
   get_deploy_history (MCP, runs under a service-account tenant
   context) to its env-var fallback.

3. audit_logs immutability moves from a model docstring to the
   database: RESTRICTIVE deny policies for UPDATE and DELETE.
   Restrictive policies AND with the permissive tenant_isolation
   policy, so — once FORCE is on — they bind owner and app role alike:
   both commands become silent no-ops ('UPDATE 0'). Referential-
   integrity actions bypass row security, so the ON DELETE SET NULL FKs
   from jobs/users still null audit_logs.job_id/user_id when those rows
   are deleted (scripts/reset_eval_state.py depends on that; a
   raising trigger would have broken it). Deliberately NOT applied to
   job_events or outbox_events — the outbox relay UPDATEs
   outbox_events.published_at, and job_events is out of scope here.

Deliberately without a policy: users (auth must read it before the
request's app.tenant_id exists — the ADR 0003 bootstrap; the unit gate
backend/tests/unit/test_rls_coverage.py encodes it as the single
allowed exclusion) and tenants / job_dependencies /
service_account_tokens (no tenant_id column).

CREATE POLICY has no IF NOT EXISTS; this is a plain one-shot migration
(Alembic guarantees single execution) — no idempotence guards that
could mask a half-applied downgrade.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a7e3d9c41f28"
down_revision: str | Sequence[str] | None = "f4a7d2c85b19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The six tables c4f8e9a52340 already covers, mirrored here for the FORCE
# pass (merged migrations are frozen history and versions/ is not a
# package, so the list is copied rather than imported).
_PRIOR_TABLES = [
    "jobs",
    "audit_logs",
    "outbox_events",
    "job_events",
    "sagas",
    "job_triages",
]

# Tenant tables created after c4f8e9a52340 with no policy at all. Same
# policy text as c4f8e9a52340 (unset-tenant escape hatch preserved).
_NEW_TABLES = [
    "incident_summaries",
    "service_accounts",
    "alerts",
    "idempotency_records",
]

# Same shape plus `OR tenant_id IS NULL`: platform-wide rows (tenant_id
# nullable by design) must stay visible under tenant-scoped sessions.
_NULL_TENANT_TABLES = [
    "deploy_markers",
]

_ALL_TABLES = _PRIOR_TABLES + _NEW_TABLES + _NULL_TENANT_TABLES

_POLICY = """
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

_NULL_TENANT_POLICY = """
    CREATE POLICY tenant_isolation ON {table}
      USING (
        current_setting('app.tenant_id', true) IS NULL
        OR current_setting('app.tenant_id', true) = ''
        OR tenant_id IS NULL
        OR tenant_id = current_setting('app.tenant_id', true)::uuid
      )
      WITH CHECK (
        current_setting('app.tenant_id', true) IS NULL
        OR current_setting('app.tenant_id', true) = ''
        OR tenant_id IS NULL
        OR tenant_id = current_setting('app.tenant_id', true)::uuid
      )
"""


def upgrade() -> None:
    # Skip on non-Postgres dialects (SQLite test DB ignores this).
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table in _NEW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(_POLICY.format(table=table))

    for table in _NULL_TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(_NULL_TENANT_POLICY.format(table=table))

    # FORCE subjects the table owner to the policies as well — the switch
    # that makes RLS real for the RDS-master connection in production.
    for table in _ALL_TABLES:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # DB-level audit immutability. USING (false) leaves no row visible to
    # UPDATE/DELETE, so both are silent no-ops (command tag 'UPDATE 0'),
    # not errors. RI actions (the SET NULL FKs) bypass row security.
    op.execute(
        "CREATE POLICY audit_logs_block_update ON audit_logs"
        " AS RESTRICTIVE FOR UPDATE USING (false)"
    )
    op.execute(
        "CREATE POLICY audit_logs_block_delete ON audit_logs"
        " AS RESTRICTIVE FOR DELETE USING (false)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("DROP POLICY IF EXISTS audit_logs_block_delete ON audit_logs")
    op.execute("DROP POLICY IF EXISTS audit_logs_block_update ON audit_logs")

    for table in _ALL_TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")

    for table in _NEW_TABLES + _NULL_TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
