"""multi tenancy: tenants table + tenant_id on all tenant-scoped tables

Revision ID: f8a1c4e23507
Revises: e7f4c2a91b08
Create Date: 2026-06-28 12:00:00.000000

Migration strategy
==================
1. Create `tenants` table.
2. Insert a default tenant (UUID is fixed so subsequent migrations / fixtures
   can reference it).
3. For each tenant-scoped table:
   a. Add a nullable `tenant_id` column.
   b. UPDATE every existing row to point at the default tenant.
   c. ALTER the column to NOT NULL and add the FK constraint + index.

The two-phase add-then-NOT-NULL is needed because Postgres requires every
existing row to have a value before NOT NULL can be enforced.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8a1c4e23507"
down_revision: str | Sequence[str] | None = "e7f4c2a91b08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Hardcoded so it's stable across environments and reproducible in tests.
DEFAULT_TENANT_ID = "d3fa17de-7a17-de7a-17de-7a17de7a17de"


# Order matters here for the data-layer FK: every other tenant-scoped table
# eventually depends on this set being populated first.
_TENANT_SCOPED_TABLES = [
    "users",
    "jobs",
    "audit_logs",
    "outbox_events",
    "job_events",
    "sagas",
    "job_triages",
]


def upgrade() -> None:
    # 1. tenants
    op.create_table(
        "tenants",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_tenants_slug"),
    )
    op.create_index(op.f("ix_tenants_slug"), "tenants", ["slug"], unique=True)

    # 2. seed the default tenant
    op.execute(
        sa.text(
            "INSERT INTO tenants (id, slug, name, is_active) "
            f"VALUES ('{DEFAULT_TENANT_ID}', 'default', 'Default Tenant', true)"
        )
    )

    # 3. for each tenant-scoped table: add nullable → backfill → NOT NULL + FK
    for table in _TENANT_SCOPED_TABLES:
        op.add_column(table, sa.Column("tenant_id", sa.UUID(), nullable=True))
        op.execute(
            sa.text(
                f"UPDATE {table} SET tenant_id = '{DEFAULT_TENANT_ID}' "
                "WHERE tenant_id IS NULL"
            )
        )
        op.alter_column(table, "tenant_id", nullable=False)
        op.create_foreign_key(
            f"fk_{table}_tenant_id_tenants",
            table,
            "tenants",
            ["tenant_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_index(
            f"ix_{table}_tenant_id", table, ["tenant_id"], unique=False
        )


def downgrade() -> None:
    for table in reversed(_TENANT_SCOPED_TABLES):
        op.drop_index(f"ix_{table}_tenant_id", table_name=table)
        op.drop_constraint(
            f"fk_{table}_tenant_id_tenants", table, type_="foreignkey"
        )
        op.drop_column(table, "tenant_id")
    op.drop_index(op.f("ix_tenants_slug"), table_name="tenants")
    op.drop_table("tenants")
