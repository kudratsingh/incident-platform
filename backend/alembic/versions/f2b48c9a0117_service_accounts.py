"""service accounts + tokens

Revision ID: f2b48c9a0117
Revises: e1d24a8b50c2
Create Date: 2026-07-15 09:00:00.000000

Wave 1 PR #1 of the agent-platform program. Introduces the machine-principal
shape: `service_accounts` (durable, tenant-scoped, carries a maximum scope
set) and `service_account_tokens` (SHA-256 hashes of `sa_<random>` bearer
tokens, each with its own scope subset, expiry, and revocation).

The unique index on token_hash is the hot lookup — every request that
presents an sa_ token hashes it once and looks up the row. Composite index
on (tenant_id, name) lets operators find their tenant's accounts quickly
and enforces per-tenant name uniqueness.

Rationale: docs/ADR/0007-machine-principal-scope-model.md.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "f2b48c9a0117"
down_revision: str | Sequence[str] | None = "e1d24a8b50c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_accounts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("scopes", JSONB(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
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
        sa.UniqueConstraint(
            "tenant_id", "name", name="uq_service_accounts_tenant_name"
        ),
    )
    op.create_index(
        "ix_service_accounts_tenant_id", "service_accounts", ["tenant_id"]
    )

    op.create_table(
        "service_account_tokens",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "service_account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("service_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("scopes", JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # Hot lookup: every authenticated request with an sa_ token hashes it
    # and probes this index.
    op.create_index(
        "ix_service_account_tokens_token_hash",
        "service_account_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_service_account_tokens_service_account_id",
        "service_account_tokens",
        ["service_account_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_service_account_tokens_service_account_id", "service_account_tokens"
    )
    op.drop_index(
        "ix_service_account_tokens_token_hash", "service_account_tokens"
    )
    op.drop_table("service_account_tokens")
    op.drop_index("ix_service_accounts_tenant_id", "service_accounts")
    op.drop_table("service_accounts")
