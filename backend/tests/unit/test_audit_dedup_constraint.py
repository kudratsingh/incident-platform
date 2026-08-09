"""Real-constraint proof for uq_audit_logs_kafka_coord.

Uses the conftest SQLite session — SQLite enforces UNIQUE, so this is a
genuine constraint test, not a mock. Two properties matter:

1. Two rows with the same (kafka_topic, kafka_partition, kafka_offset)
   collide — the dedup primitive the AuditConsumer relies on under
   at-least-once redelivery.
2. Rows with all-NULL coordinates never collide (NULLs are distinct under
   UNIQUE in both SQLite and Postgres) — this protects the inline
   transactional audit writers (API routes, worker, saga coordinator),
   which carry no Kafka coordinates.
"""

import pytest
from app.models.audit import AuditLog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


async def test_duplicate_kafka_coords_raise_integrity_error(
    db_session: AsyncSession, default_tenant
) -> None:
    db_session.add(
        AuditLog(
            tenant_id=default_tenant.id,
            action="event.job.completed",
            kafka_topic="job.completed",
            kafka_partition=0,
            kafka_offset=7,
        )
    )
    await db_session.flush()

    # Redelivery of the same Kafka message → identical coordinates.
    db_session.add(
        AuditLog(
            tenant_id=default_tenant.id,
            action="event.job.completed",
            kafka_topic="job.completed",
            kafka_partition=0,
            kafka_offset=7,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_null_coords_rows_never_collide(
    db_session: AsyncSession, default_tenant
) -> None:
    """Inline application writers leave the coords NULL — unlimited NULL
    rows must coexist under the UNIQUE constraint."""
    db_session.add(
        AuditLog(
            tenant_id=default_tenant.id,
            action="job.created",
            kafka_topic=None,
            kafka_partition=None,
            kafka_offset=None,
        )
    )
    db_session.add(
        AuditLog(
            tenant_id=default_tenant.id,
            action="job.created",
            kafka_topic=None,
            kafka_partition=None,
            kafka_offset=None,
        )
    )
    await db_session.flush()

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "job.created", AuditLog.kafka_topic.is_(None))
        )
    ).scalar_one()
    assert count == 2
