"""
Event log consumer — appends every Kafka lifecycle event into the
`job_events` table. This is the event-sourcing store: replaying the rows
for a job_id in recorded_at order reconstructs its full state history.

Idempotency under at-least-once delivery:
  - The table has UNIQUE (kafka_topic, kafka_partition, kafka_offset).
  - On redelivery the INSERT fails the constraint, we swallow the
    IntegrityError, and the consumer commits the offset as success.
  - This keeps the log clean without any application-level dedup state.
"""

import uuid
from typing import Any

from app.config import get_settings
from app.core.logging import get_logger
from app.models.event_log import JobEvent
from app.workers.kafka_consumer import BaseKafkaConsumer
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = get_logger(__name__)


class EventLogConsumer(BaseKafkaConsumer):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        settings = get_settings()
        super().__init__(
            topics=[
                settings.kafka_topic_job_submitted,
                settings.kafka_topic_job_progress,
                settings.kafka_topic_job_completed,
                settings.kafka_topic_job_failed,
                settings.kafka_topic_job_dlq,
            ],
            group_id=settings.kafka_consumer_group_event_log,
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
                "event-log skipping unparseable message",
                extra={"topic": topic, "partition": partition, "offset": offset},
            )
            return

        job_id_str = value.get("job_id")
        job_id: uuid.UUID | None = None
        if isinstance(job_id_str, str):
            try:
                job_id = uuid.UUID(job_id_str)
            except ValueError:
                pass

        event = JobEvent(
            job_id=job_id,
            event_name=str(event_name),
            payload=dict(value),
            kafka_topic=topic,
            kafka_partition=partition,
            kafka_offset=offset,
        )
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    session.add(event)
        except IntegrityError:
            # Redelivery — row already exists. Not an error.
            logger.debug(
                "event-log dedup skipped duplicate",
                extra={"topic": topic, "partition": partition, "offset": offset},
            )
