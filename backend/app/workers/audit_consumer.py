"""
Audit consumer — writes audit_log rows in response to Kafka lifecycle events.

This is intentionally **additive** for now: the API and worker still write
transactional audit rows inline (so the audit trail stays atomic with the
state change). This consumer writes a second class of rows tagged
`event.<name>` to demonstrate Kafka-driven consumer-group fan-out and to
provide a self-contained event-sourced view of job lifecycle. In a later
phase the inline writes can be removed once Kafka delivery is trusted.

Idempotency under at-least-once delivery: every event.* row carries its
Kafka coordinates, and audit_logs has UNIQUE (kafka_topic, kafka_partition,
kafka_offset). On redelivery the INSERT fails the constraint, we swallow
the IntegrityError, and the offset commits — same pattern as
EventLogConsumer. Inline audit writers leave the coords NULL and are
unaffected (NULLs never collide under UNIQUE).
"""

import uuid
from typing import Any

from app.config import get_settings
from app.core.logging import get_logger
from app.repositories.audit import AuditRepository
from app.workers.kafka_consumer import BaseKafkaConsumer
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = get_logger(__name__)

# Map Kafka event names to the audit action label this consumer writes.
_EVENT_TO_ACTION: dict[str, str] = {
    "job.submitted": "event.job.submitted",
    "job.completed": "event.job.completed",
    "job.failed": "event.job.failed",
}


class AuditConsumer(BaseKafkaConsumer):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        settings = get_settings()
        super().__init__(
            topics=[
                settings.kafka_topic_job_submitted,
                settings.kafka_topic_job_completed,
                settings.kafka_topic_job_failed,
                settings.kafka_topic_job_dlq,
            ],
            group_id=settings.kafka_consumer_group_audit,
        )
        self.session_factory = session_factory

    async def handle_message(
        self,
        topic: str,
        key: str | None,
        value: dict[str, Any],
        *,
        partition: int = 0,
        offset: int = 0,
    ) -> None:
        event_name = value.get("event") if isinstance(value, dict) else None
        if not event_name:
            logger.warning(
                "skipping malformed event for audit", extra={"topic": topic, "value": value}
            )
            return

        # Dead-letter messages carry event="job.failed" with dead_lettered=True.
        # Surface that distinction in the audit label.
        action: str
        if event_name == "job.failed" and value.get("dead_lettered") is True:
            action = "event.job.dead_letter"
        else:
            mapped = _EVENT_TO_ACTION.get(event_name)
            if mapped is None:
                logger.warning(
                    "no audit mapping for event", extra={"topic": topic, "event": event_name}
                )
                return
            action = mapped

        job_id = _parse_uuid(value.get("job_id"))
        user_id = _parse_uuid(value.get("user_id"))
        tenant_id = _parse_uuid(value.get("tenant_id"))
        if tenant_id is None:
            logger.warning(
                "audit consumer skipping event without tenant_id",
                extra={"topic": topic, "event": event_name},
            )
            return

        extra_data = {
            k: v
            for k, v in value.items()
            if k not in ("event", "tenant_id", "job_id", "user_id")
        }

        # The IntegrityError from uq_audit_logs_kafka_coord surfaces on the
        # transaction exit (commit), not on the .log() await — the except
        # must wrap both context managers.
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    await AuditRepository(session).log(
                        action,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        job_id=job_id,
                        resource_type="job",
                        resource_id=str(job_id) if job_id else None,
                        extra_data=extra_data,
                        kafka_topic=topic,
                        kafka_partition=partition,
                        kafka_offset=offset,
                    )
        except IntegrityError:
            # Redelivery — row already exists. Not an error; returning
            # normally lets the offset commit.
            logger.debug(
                "audit dedup skipped duplicate",
                extra={"topic": topic, "partition": partition, "offset": offset},
            )


def _parse_uuid(value: Any) -> uuid.UUID | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None
