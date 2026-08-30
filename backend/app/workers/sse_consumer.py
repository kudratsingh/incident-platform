"""
SSE broadcaster consumer — bridges Kafka lifecycle events into the per-job
Redis pub/sub channel that the SSE endpoint already subscribes to.

This decouples the dispatcher from SSE clients entirely: the dispatcher
publishes to Kafka and walks away; this consumer fans out to any number of
SSE-connected browsers via Redis pub/sub. Different consumer instance =
different subscriber group = independent processing, no coordination needed.
"""

from typing import Any

from app.config import get_settings
from app.core.logging import get_logger
from app.workers.kafka_consumer import BaseKafkaConsumer
from app.workers.progress import publish as publish_progress
from redis.asyncio import Redis

logger = get_logger(__name__)

# Map Kafka event names to the SSE status string clients see.
# `job.failed` is split based on the `dead_lettered` flag at runtime:
#   - dead_lettered=True  → "dead_letter" (terminal)
#   - dead_lettered=False → "retrying"    (transient — will be re-dispatched)
_EVENT_TO_STATUS: dict[str, str] = {
    "job.progress": "running",
    "job.completed": "completed",
}


class SseConsumer(BaseKafkaConsumer):
    def __init__(self, redis: Redis) -> None:
        settings = get_settings()
        super().__init__(
            topics=[
                settings.kafka_topic_job_progress,
                settings.kafka_topic_job_completed,
                settings.kafka_topic_job_failed,
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

        # Provenance for the snapshot's ordering guard (WO-R2-57). Every event
        # for a job carries the same Kafka key (`{tenant}:{user}`) and so
        # shares a partition, which makes the offset within one topic the
        # producer's own order — and makes a redelivered offset recognisable
        # as the replay it is. Offsets from *different* topics are
        # incomparable, hence the topic travelling with the offset.
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
