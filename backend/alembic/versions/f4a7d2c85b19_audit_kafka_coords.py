"""audit_logs kafka coordinates + redelivery dedup constraint

Revision ID: f4a7d2c85b19
Revises: e3f2c6b91d84
Create Date: 2026-08-09 10:00:00.000000

Adds nullable `kafka_topic` / `kafka_partition` / `kafka_offset` columns
to audit_logs plus UNIQUE (kafka_topic, kafka_partition, kafka_offset)
named `uq_audit_logs_kafka_coord` — the same dedup primitive job_events
carries as `uq_job_events_kafka_coord`.

Why: the AuditConsumer writes an `event.*` audit row per Kafka lifecycle
message, and offsets commit only after the handler returns (at-least-once
delivery). A redelivered message would therefore append a duplicate row
to the immutable audit trail. With this constraint the second INSERT
fails, the consumer swallows the IntegrityError, and the offset commits —
redelivery becomes a no-op.

Nullable is load-bearing: inline transactional audit writers (API routes,
worker _run_job, saga coordinator) have no Kafka coordinates and leave
all three columns NULL. Both Postgres and SQLite treat NULLs as distinct
under UNIQUE, so inline rows never collide with each other or with
consumer-written rows.

Deliberately a plain UNIQUE constraint, not a partial index — the
NULL-distinct semantics already exempt inline rows, and autogenerate
mishandles partial indexes.

audit_logs sits under RLS (c4f8e9a52340); adding nullable columns does
not touch the policies.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4a7d2c85b19"
down_revision: str | Sequence[str] | None = "e3f2c6b91d84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_logs",
        sa.Column("kafka_topic", sa.String(128), nullable=True),
    )
    op.add_column(
        "audit_logs",
        sa.Column("kafka_partition", sa.Integer(), nullable=True),
    )
    op.add_column(
        "audit_logs",
        sa.Column("kafka_offset", sa.BigInteger(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_audit_logs_kafka_coord",
        "audit_logs",
        ["kafka_topic", "kafka_partition", "kafka_offset"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_audit_logs_kafka_coord", "audit_logs", type_="unique")
    op.drop_column("audit_logs", "kafka_offset")
    op.drop_column("audit_logs", "kafka_partition")
    op.drop_column("audit_logs", "kafka_topic")
