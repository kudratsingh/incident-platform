"""
Audit consumer — writes `event.<name>` audit_log rows off Kafka lifecycle events. Deliberately
**additive**: the API and worker still write audit rows inline, atomic with the state change.

Idempotency: `audit_logs.uq_audit_logs_kafka_coord` makes a redelivery's INSERT fail, the
IntegrityError is swallowed and the offset commits. Inline writers leave the coords NULL.
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
    "job.cancelled": "event.job.cancelled",
}


class AuditConsumer(BaseKafkaConsumer):
    """Consumer group `audit-writer` — turns each lifecycle event into an
    `event.*` audit row."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        settings = get_settings()
        super().__init__(
            topics=[
                settings.kafka_topic_job_submitted,
                settings.kafka_topic_job_completed,
                settings.kafka_topic_job_failed,
                settings.kafka_topic_job_cancelled,
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
        """Write one audit row for this event. A redelivery collides on the
        Kafka coordinates and is skipped."""
        event_name = value.get("event") if isinstance(value, dict) else None
        if not event_name:
            logger.warning(
                "skipping malformed event for audit", extra={"topic": topic, "value": value}
            )
            return

        # Dead-letters arrive as job.failed + dead_lettered=True.
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

        # uq_audit_logs_kafka_coord raises on commit, so `except` must wrap both managers.
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
