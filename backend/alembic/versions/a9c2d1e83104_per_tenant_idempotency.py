"""per-tenant idempotency key uniqueness

Revision ID: a9c2d1e83104
Revises: f8a1c4e23507
Create Date: 2026-06-29 00:00:00.000000

The original `jobs.idempotency_key` had a global UNIQUE index. With
multi-tenancy enforced, that prevents two tenants from reusing the same
caller-supplied idempotency key — which would be a surprising cross-tenant
collision. Replace the single-column UNIQUE with a composite UNIQUE on
(tenant_id, idempotency_key) so each tenant has its own keyspace.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a9c2d1e83104"
down_revision: str | Sequence[str] | None = "f8a1c4e23507"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop the auto-named unique constraint that came with the original
    # column declaration. The Postgres index name follows the pattern
    # ix_jobs_idempotency_key (unique=True puts it in pg_indexes).
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("jobs_idempotency_key_key", type_="unique")
    op.create_unique_constraint(
        "uq_jobs_tenant_idempotency_key",
        "jobs",
        ["tenant_id", "idempotency_key"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_jobs_tenant_idempotency_key", "jobs", type_="unique")
    op.create_unique_constraint(
        "jobs_idempotency_key_key", "jobs", ["idempotency_key"]
    )
