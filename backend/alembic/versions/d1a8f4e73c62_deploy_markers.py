"""deploy markers

Revision ID: d1a8f4e73c62
Revises: c8f1e2b39d47
Create Date: 2026-07-28 10:00:00.000000

Persistent record of deploys. `get_deploy_history` MCP tool reads
from this table when populated, falling back to env vars for the
single-entry "what's live right now" case.

Rows are written by the release pipeline (future) and by
`scripts/seed_eval_fixtures.py` for eval scenarios that hypothesise
about deploy-correlated failures.

Index on `(environment, deployed_at DESC)` — the exact scan the tool
runs when listing recent deploys per env.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "d1a8f4e73c62"
down_revision: str | Sequence[str] | None = "c8f1e2b39d47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deploy_markers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # Nullable — deploys are platform-wide, not tenant-scoped. The
        # column exists so a future per-tenant deploy story has somewhere
        # to hang. All current writes leave it null.
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("revision", sa.String(64), nullable=True),
        sa.Column("image_tag", sa.String(128), nullable=True),
        sa.Column("environment", sa.String(32), nullable=False),
        sa.Column(
            "deployed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Free-form annotation. Populated by seed script with correlation
        # hints like "correlated with billing failures" so the eval
        # scenario probing deploy-correlation gets meaningful text.
        sa.Column("notes", sa.String(1024), nullable=True),
        sa.Column("extra_data", JSONB(), nullable=True),
    )
    op.create_index(
        "ix_deploy_markers_env_deployed_at",
        "deploy_markers",
        ["environment", sa.text("deployed_at DESC")],
    )
    op.create_index(
        "ix_deploy_markers_deployed_at",
        "deploy_markers",
        [sa.text("deployed_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_deploy_markers_deployed_at", table_name="deploy_markers")
    op.drop_index(
        "ix_deploy_markers_env_deployed_at", table_name="deploy_markers"
    )
    op.drop_table("deploy_markers")
