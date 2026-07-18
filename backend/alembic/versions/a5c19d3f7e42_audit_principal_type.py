"""audit principal_type + principal_id

Revision ID: a5c19d3f7e42
Revises: f2b48c9a0117
Create Date: 2026-07-15 15:00:00.000000

Wave 1 PR #2 of the agent-platform program. Operator audit log.

Before this migration every audit_logs row identified the actor via
`user_id` (FK → users). Machine principals (service accounts) can't use
that FK — so add `principal_type` ('user' | 'service_account') plus a
plain-UUID `principal_id` that points at whichever table the type
selects. Kept the existing `user_id` column for backward compatibility;
new writes for human actors set both `user_id` and `principal_id` to
the same value so old queries continue to work.

Backfill: every existing row is set to principal_type='user',
principal_id=user_id. Rows with a null user_id (audit entries the
system wrote without a specific actor) keep principal_type='user'
with null principal_id — no impact on downstream code.

The composite index (tenant_id, principal_type, created_at DESC) is
what the admin Audit tab's principal-type filter will scan. Existing
indexes on individual columns are untouched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a5c19d3f7e42"
down_revision: str | Sequence[str] | None = "f2b48c9a0117"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add nullable-first so the backfill can run without violating the
    # constraint, then tighten to NOT NULL with a server_default.
    op.add_column(
        "audit_logs",
        sa.Column("principal_type", sa.String(32), nullable=True),
    )
    op.add_column(
        "audit_logs",
        sa.Column("principal_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )

    # Backfill. Every existing row is a human actor (this migration ships
    # before any machine-principal audit rows exist). principal_id may be
    # null where user_id was null (system-emitted audit rows).
    op.execute(
        "UPDATE audit_logs SET principal_type = 'user', principal_id = user_id "
        "WHERE principal_type IS NULL"
    )

    # Now tighten. server_default keeps future inserts working for callers
    # we haven't updated yet — the AuditRepository update in this PR sets
    # it explicitly for both paths, but the default is a belt-and-braces
    # guard against a caller I miss.
    op.alter_column(
        "audit_logs",
        "principal_type",
        existing_type=sa.String(32),
        nullable=False,
        server_default="user",
    )

    op.create_index(
        "ix_audit_logs_tenant_principal_type_created",
        "audit_logs",
        ["tenant_id", "principal_type", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_audit_logs_tenant_principal_type_created", table_name="audit_logs"
    )
    op.drop_column("audit_logs", "principal_id")
    op.drop_column("audit_logs", "principal_type")
