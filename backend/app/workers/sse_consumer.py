"""
SSE broadcaster consumer — bridges Kafka lifecycle events into the per-job Redis pub/sub channel
the SSE endpoint subscribes to, so the dispatcher publishes to Kafka and walks away.
"""

from typing import Any

from app.config import get_settings
from app.core.logging import get_logger
from app.workers.kafka_consumer import BaseKafkaConsumer
from app.workers.progress import publish as publish_progress
from redis.asyncio import Redis

logger = get_logger(__name__)

# Kafka event name → the SSE status clients see. `job.failed` splits on `dead_lettered`: True is
# terminal "dead_letter", False transient "retrying". `cancelled` got a producer at WO-R2-113.
_EVENT_TO_STATUS: dict[str, str] = {
    "job.progress": "running",
    "job.completed": "completed",
    "job.cancelled": "cancelled",
}


class SseConsumer(BaseKafkaConsumer):
    """Consumer group `sse-broadcaster` — republishes lifecycle events onto the
    per-job Redis channel the browser's progress stream reads."""

    def __init__(self, redis: Redis) -> None:
        settings = get_settings()
        super().__init__(
            topics=[
                settings.kafka_topic_job_progress,
                settings.kafka_topic_job_completed,
                settings.kafka_topic_job_failed,
                settings.kafka_topic_job_cancelled,
                settings.kafka_topic_job_dlq,
            ],
            group_id=settings.kafka_consumer_group_sse,
        )
        self.redis = redis

    async def handle_message(
        self,
        topic: str,
        key: str | None,
        value: dict[str, Any],
        **kafka_meta: Any,
    ) -> None:
        """Translate one lifecycle event into the status the browser shows and
        publish it, carrying the topic and offset so stale events can be
        recognised."""
        if not isinstance(value, dict):
            logger.warning("skipping non-dict SSE event", extra={"topic": topic})
            return

        job_id = value.get("job_id")
        event_name = value.get("event")
        if not job_id or not event_name:
            logger.warning(
                "skipping malformed SSE event",
                extra={"topic": topic, "value": value},
            )
            return

        status: str
        if event_name == "job.failed":
            status = "dead_letter" if value.get("dead_lettered") is True else "retrying"
        else:
            mapped = _EVENT_TO_STATUS.get(event_name)
            if mapped is None:
                return  # unknown event type — silently skip
            status = mapped

        # job.progress carries percent/message/retry_count; terminal events use defaults.
        percent = int(value.get("percent", 100 if status == "completed" else 0))
        message = str(
            value.get("message")
            or (
                "Job completed successfully"
                if status == "completed"
                else f"Job {status}"
            )
        )
        retry_count = int(value.get("retry_count", 0))

        # Provenance for the snapshot's ordering guard (WO-R2-57): within one topic the offset is
        # the producer's order, and offsets across topics are incomparable — hence the topic too.
        offset = kafka_meta.get("offset")
        await publish_progress(
            self.redis,
            job_id=str(job_id),
            status=status,
            progress=percent,
            message=message,
            retry_count=retry_count,
            source=topic,
            sequence=int(offset) if offset is not None else None,
        )
