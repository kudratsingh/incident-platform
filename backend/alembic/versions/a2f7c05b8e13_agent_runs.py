"""agent_runs — the responder's own report of a run, operator-readable only

Revision ID: a2f7c05b8e13
Revises: f1c7b93a4d26
Create Date: 2026-09-19 05:10:00.000000

WO-R3-312 / ADR 0035. The console needs one source of truth for "what is the
responder doing" and "what did it conclude". Neither existed: everything the
responder knew lived in its own process, and the only platform trace of it was
one `agent.tool_invoked` row per call.

Shape notes, in column order:

- `id` is **supplied by the caller**, not defaulted here. `report_agent_run` is
  an upsert by run id, so the caller's id has to be the primary key or a repeat
  report would need a content search to find its own row.

- `alert_id` is `ON DELETE SET NULL`. The record of a run outlives the alert
  that started it; losing the link is better than losing the run or pinning the
  alert.

- `service_account_id` is a real FK, unlike `audit_logs.principal_id`, which
  has none because the same column there may name either `users.id` or
  `service_accounts.id` (ADR 0007). This column can only ever name a service
  account, so the FK costs nothing and buys referential integrity. RESTRICT,
  not CASCADE: deleting a principal must not delete the history of what it did.

- `phase_history` is NOT NULL with a server default of `[]`, so a row always
  has a list to append to and no reader has to treat NULL as empty.

- `briefing` is nullable and written exactly once; the "once" is enforced in
  the service layer (a second write is a 409), not by a constraint, because
  "NULL → value, never value → value" is not expressible as one.

RLS
===
`_TABLES` below is the declaration `backend/tests/unit/test_rls_coverage.py`
reads: that gate unions the `_TABLES` / `_NEW_TABLES` / `_NULL_TENANT_TABLES`
attributes of every module in `versions/` and fails if any model carrying
`tenant_id` is missing from the union. The policy text is the strict predicate
e2a9c4f70b31 shipped (ADR 0026) — unset or empty `app.tenant_id` denies rather
than admits — plus FORCE, so the owner connection is bound by it too
(ADR 0015). `backend/tests/integration/test_rls_enforcement.py` derives its
table list from the ORM, so it covers this table with no edit.

Grants
======
None needed: b8e4a1c92f35's `ALTER DEFAULT PRIVILEGES` gives `incident_app`
full DML on tables created later by the migration role. The table is *not*
immutable — a run is updated on every report — so, unlike `audit_logs`, it
takes no REVOKE and no RESTRICTIVE deny policy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "a2f7c05b8e13"
down_revision: str | Sequence[str] | None = "f1c7b93a4d26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Read by the plain-CI RLS coverage gate — see the module docstring. A rename
# here reads as "this table lost its policy".
_TABLES = ["agent_runs"]

# The strict predicate from e2a9c4f70b31 (ADR 0026), copied rather than
# imported: merged migrations are frozen history and `versions/` is not a
# package.
_MATCH = (
    "current_setting('app.tenant_scope', true) = 'platform'"
    " OR tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        # No default: the caller's run id is the key (see docstring).
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "alert_id",
            UUID(as_uuid=True),
            sa.ForeignKey("alerts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "service_account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("service_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("scenario", sa.String(128), nullable=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column(
            "phase_history",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("current_hypothesis", JSONB(), nullable=True),
        sa.Column("last_step", JSONB(), nullable=True),
        sa.Column("briefing", JSONB(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_agent_runs_tenant_id", "agent_runs", ["tenant_id"])
    op.create_index("ix_agent_runs_alert_id", "agent_runs", ["alert_id"])
    op.create_index(
        "ix_agent_runs_service_account_id", "agent_runs", ["service_account_id"]
    )
    op.create_index("ix_agent_runs_state", "agent_runs", ["state"])
    # The console's own query, twice a second while a demo is running: the
    # unfinished runs for one tenant, newest first. Partial, like
    # `ix_alerts_active`, so it stays small as finished runs accumulate.
    op.create_index(
        "ix_agent_runs_active",
        "agent_runs",
        ["tenant_id", sa.text("started_at DESC")],
        postgresql_where=sa.text("finished_at IS NULL"),
    )

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON agent_runs"
        f"  USING ({_MATCH})"
        f"  WITH CHECK ({_MATCH})"
    )
    op.execute("ALTER TABLE agent_runs FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE agent_runs NO FORCE ROW LEVEL SECURITY")
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON agent_runs")
        op.execute("ALTER TABLE agent_runs DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_agent_runs_active", table_name="agent_runs")
    op.drop_index("ix_agent_runs_state", table_name="agent_runs")
    op.drop_index("ix_agent_runs_service_account_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_alert_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_tenant_id", table_name="agent_runs")
    op.drop_table("agent_runs")
