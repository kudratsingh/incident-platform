"""idempotency records

Revision ID: c8f1e2b39d47
Revises: b6d2f8c3a541
Create Date: 2026-07-17 22:00:00.000000

Wave 3 PR E. Persistent record of every Tier 1 action executed on
behalf of a machine principal, keyed by the caller-supplied
`idempotency_key`. On a repeat call with the same
(tenant, principal, key):
  - matching arguments_hash → return the cached response verbatim
  - differing arguments_hash → refuse (409) so a mis-reused key
    doesn't silently execute a different action

Records expire after TTL. Only the record row expires — the side
effects the tool caused don't, and can't (a `restart_consumer_group`
already ran).

Composite UNIQUE (tenant_id, principal_id, idempotency_key) makes
the fast-path lookup a single index probe. Partial index on
`expires_at IS NOT NULL` for the reaper.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "c8f1e2b39d47"
down_revision: str | Sequence[str] | None = "b6d2f8c3a541"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_records",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # Not FK-constrained — service accounts (Wave 1) live in a
        # different table than users (existing) and the plain-UUID
        # convention already used on `audit_logs.principal_id` fits.
        sa.Column("principal_id", UUID(as_uuid=True), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        # SHA-256 hex of canonical-JSON arguments. Used to detect
        # "same key, different args" mis-reuse.
        sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("response_json", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Nullable = never expires. Tools may pick their own TTL.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "principal_id",
            "idempotency_key",
            name="uq_idempotency_scope",
        ),
    )
    op.create_index(
        "ix_idempotency_records_tenant_id",
        "idempotency_records",
        ["tenant_id"],
    )
    # Reaper scan path.
    op.create_index(
        "ix_idempotency_records_expires",
        "idempotency_records",
        ["expires_at"],
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_idempotency_records_expires", table_name="idempotency_records"
    )
    op.drop_index(
        "ix_idempotency_records_tenant_id", table_name="idempotency_records"
    )
    op.drop_table("idempotency_records")
