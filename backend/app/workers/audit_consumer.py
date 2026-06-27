"""
Audit consumer — writes audit_log rows in response to Kafka lifecycle events.

This is intentionally **additive** for now: the API and worker still write
transactional audit rows inline (so the audit trail stays atomic with the
state change). This consumer writes a second class of rows tagged
`event.<name>` to demonstrate Kafka-driven consumer-group fan-out and to
provide a self-contained event-sourced view of job lifecycle. In a later
phase the inline writes can be removed once Kafka delivery is trusted.
"""

import uuid
from typing import Any

from app.config import get_settings
from app.core.logging import get_logger
from app.repositories.audit import AuditRepository
from app.workers.kafka_consumer import BaseKafkaConsumer
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
        **_kafka_meta: Any,
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

        extra_data = {
            k: v
            for k, v in value.items()
            if k not in ("event", "job_id", "user_id")
        }

        async with self.session_factory() as session:
            async with session.begin():
                await AuditRepository(session).log(
                    action,
                    user_id=user_id,
                    job_id=job_id,
                    resource_type="job",
                    resource_id=str(job_id) if job_id else None,
                    extra_data=extra_data,
                )


def _parse_uuid(value: Any) -> uuid.UUID | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None
