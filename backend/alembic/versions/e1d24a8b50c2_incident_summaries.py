"""incident summaries

Revision ID: e1d24a8b50c2
Revises: d9c01a7e4f30
Create Date: 2026-06-29 16:30:00.000000

One row per (tenant, window) that the digest worker has summarized.
Cheap to write (background loop), heavy reads (every admin opening the
Digests tab), so it's worth persisting rather than recomputing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "e1d24a8b50c2"
down_revision: str | Sequence[str] | None = "d9c01a7e4f30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "incident_summaries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("highlights", JSONB(), nullable=True),
        sa.Column("model_used", sa.String(64), nullable=False),
        sa.Column("usage", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # Most queries fetch the latest digests for one tenant in time order.
    op.create_index(
        "ix_incident_summaries_tenant_window",
        "incident_summaries",
        ["tenant_id", sa.text("window_end DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_incident_summaries_tenant_window", "incident_summaries")
    op.drop_table("incident_summaries")
