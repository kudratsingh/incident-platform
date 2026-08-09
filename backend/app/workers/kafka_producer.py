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

from aiokafka import AIOKafkaProducer  # type: ignore[import-untyped]
from app.config import get_settings
from app.core.logging import get_logger
from app.workers.schema_registry import SchemaValidationError
from app.workers.schema_registry import validate as validate_schema

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
        # Broker-side dedup of producer retries (requires acks="all"): a
        # network-level resend of the same batch can no longer append twice.
        # This covers ONLY broker-retry duplicates — app-level duplicates
        # (outbox relay crash window, resolver-vs-resume-sweep race) still
        # happen and are made safe by the dispatcher's atomic
        # PENDING->RUNNING claim (JobRepository.claim_for_running).
        enable_idempotence=True,
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
        validate_schema(topic, payload)
        await _get_producer().send_and_wait(topic, value=payload, key=key)
    except SchemaValidationError as exc:
        # Schema violations are *our* bug, not the broker's — log loudly and drop.
        # Never send malformed events to consumers.
        logger.error(
            "kafka publish skipped — schema invalid",
            extra={"topic": topic, "key": key, "error": str(exc)},
        )
    except Exception as exc:
        logger.error(
            "kafka publish failed",
            extra={"topic": topic, "key": key, "error": str(exc)},
        )


async def publish_raw(topic: str, key: str, payload: dict[str, Any]) -> None:
    """Send a message and propagate errors. Used by the outbox relay so it can
    leave the row unpublished on failure and retry on the next tick. Schema
    violations raise SchemaValidationError so the relay marks the row failed
    rather than republishing forever."""
    validate_schema(topic, payload)
    await _get_producer().send_and_wait(topic, value=payload, key=key)


async def publish_job_progress(
    job_id: uuid.UUID,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    status: str,
    percent: int,
    message: str,
    retry_count: int = 0,
) -> None:
    settings = get_settings()
    await _publish(
        topic=settings.kafka_topic_job_progress,
        key=f"{tenant_id}:{user_id}",
        payload={
            "event": "job.progress",
            "tenant_id": str(tenant_id),
            "job_id": str(job_id),
            "user_id": str(user_id),
            "status": status,
            "percent": percent,
            "message": message,
            "retry_count": retry_count,
        },
    )


