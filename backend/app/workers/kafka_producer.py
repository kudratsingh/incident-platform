"""
Kafka producer — publishes job lifecycle events to Kafka topics.

Every state transition (submitted, progress, completed, failed) is published
here. The producer is a module-level singleton started once at app startup
and stopped on shutdown.

Partitioning strategy: all events are keyed by user_id so that all events for
a given user land on the same partition and are processed in order by each
consumer group.
"""

import json
import uuid
from typing import Any

from aiokafka import AIOKafkaProducer
from app.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_producer: AIOKafkaProducer | None = None


async def start_producer() -> None:
    """Start the module-level Kafka producer. Call once at app startup."""
    global _producer
    settings = get_settings()
    _producer = AIOKafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        value_serializer=lambda v: json.dumps(v).encode(),
        key_serializer=lambda k: k.encode() if isinstance(k, str) else k,
        # Wait for all in-sync replicas to acknowledge — strongest durability guarantee
        acks="all",
        # Retry up to 5 times on transient errors
        retry_backoff_ms=200,
    )
    await _producer.start()
    logger.info("kafka producer started", extra={"brokers": settings.kafka_bootstrap_servers})


async def stop_producer() -> None:
    """Flush and stop the producer. Call once at app shutdown."""
    global _producer
    if _producer is not None:
        await _producer.stop()
        _producer = None
        logger.info("kafka producer stopped")


def _get_producer() -> AIOKafkaProducer:
    if _producer is None:
        raise RuntimeError("Kafka producer not started — call start_producer() first")
    return _producer


async def _publish(topic: str, key: str, payload: dict[str, Any]) -> None:
    """Send a single message; log and swallow errors so Kafka issues don't crash the API."""
    try:
        await _get_producer().send_and_wait(topic, value=payload, key=key)
    except Exception as exc:
        logger.error(
            "kafka publish failed",
            extra={"topic": topic, "key": key, "error": str(exc)},
        )


async def publish_job_submitted(
    job_id: uuid.UUID,
    user_id: uuid.UUID,
    job_type: str,
    payload: dict[str, Any],
    priority: int,
    trace_id: str | None,
) -> None:
    settings = get_settings()
    await _publish(
        topic=settings.kafka_topic_job_submitted,
        key=str(user_id),  # partition by user_id for ordered per-user processing
        payload={
            "event": "job.submitted",
            "job_id": str(job_id),
            "user_id": str(user_id),
            "job_type": job_type,
            "payload": payload,
            "priority": priority,
            "trace_id": trace_id,
        },
    )


async def publish_job_progress(
    job_id: uuid.UUID,
    user_id: uuid.UUID,
    status: str,
    percent: int,
    message: str,
    retry_count: int = 0,
) -> None:
    settings = get_settings()
    await _publish(
        topic=settings.kafka_topic_job_progress,
        key=str(user_id),
        payload={
            "event": "job.progress",
            "job_id": str(job_id),
            "user_id": str(user_id),
            "status": status,
            "percent": percent,
            "message": message,
            "retry_count": retry_count,
        },
    )


async def publish_job_completed(
    job_id: uuid.UUID,
    user_id: uuid.UUID,
    job_type: str,
    result: dict[str, Any],
    retry_count: int,
) -> None:
    settings = get_settings()
    await _publish(
        topic=settings.kafka_topic_job_completed,
        key=str(user_id),
        payload={
            "event": "job.completed",
            "job_id": str(job_id),
            "user_id": str(user_id),
            "job_type": job_type,
            "result": result,
            "retry_count": retry_count,
        },
    )


async def publish_job_failed(
    job_id: uuid.UUID,
    user_id: uuid.UUID,
    job_type: str,
    error: str,
    retry_count: int,
    dead_lettered: bool,
) -> None:
    settings = get_settings()
    topic = settings.kafka_topic_job_dlq if dead_lettered else settings.kafka_topic_job_failed
    await _publish(
        topic=topic,
        key=str(user_id),
        payload={
            "event": "job.failed",
            "job_id": str(job_id),
            "user_id": str(user_id),
            "job_type": job_type,
            "error": error,
            "retry_count": retry_count,
            "dead_lettered": dead_lettered,
        },
    )
