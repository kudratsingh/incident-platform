"""job events

Revision ID: c3e9f1a4d802
Revises: b2a8f9c7e103
Create Date: 2026-06-26 00:01:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3e9f1a4d802"
down_revision: str | Sequence[str] | None = "b2a8f9c7e103"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("event_name", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("kafka_topic", sa.String(length=128), nullable=False),
        sa.Column("kafka_partition", sa.Integer(), nullable=False),
        sa.Column("kafka_offset", sa.BigInteger(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "kafka_topic",
            "kafka_partition",
            "kafka_offset",
            name="uq_job_events_kafka_coord",
        ),
    )
    op.create_index(op.f("ix_job_events_job_id"), "job_events", ["job_id"], unique=False)
    op.create_index(
        "ix_job_events_job_recorded",
        "job_events",
        ["job_id", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_job_events_job_recorded", table_name="job_events")
    op.drop_index(op.f("ix_job_events_job_id"), table_name="job_events")
    op.drop_table("job_events")
