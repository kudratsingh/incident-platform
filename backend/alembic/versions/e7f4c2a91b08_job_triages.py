"""job triages

Revision ID: e7f4c2a91b08
Revises: d4b1a8e60305
Create Date: 2026-06-28 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e7f4c2a91b08"
down_revision: str | Sequence[str] | None = "d4b1a8e60305"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_triages",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("root_cause_category", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("suggested_fix", sa.Text(), nullable=False),
        sa.Column("is_retryable", sa.Boolean(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("model_used", sa.String(length=64), nullable=False),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", name="uq_job_triages_job_id"),
    )
    op.create_index(
        op.f("ix_job_triages_job_id"), "job_triages", ["job_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_job_triages_job_id"), table_name="job_triages")
    op.drop_table("job_triages")
