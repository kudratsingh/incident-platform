"""
Kafka producer for job lifecycle events — a module-level singleton. If the broker is unreachable at
boot the singleton stays unset and the publish paths retry the start (throttled), so a boot-time
outage self-heals. The key carries the user, so one user's events share a partition, in order.
"""

import asyncio
import json
import time
import uuid
from typing import Any

from aiokafka import AIOKafkaProducer  # type: ignore[import-untyped]
from app.config import get_settings
from app.core.logging import get_logger
from app.workers.schema_registry import SchemaValidationError
from app.workers.schema_registry import validate as validate_schema

logger = get_logger(__name__)

_producer: AIOKafkaProducer | None = None

# Lazy-restart throttle: a start against a down broker costs a full connection timeout.
_START_RETRY_MIN_INTERVAL = 5.0
_last_start_attempt: float = 0.0

# Re-created whenever the running loop changes: a module-level `asyncio.Lock` binds to the loop
# that first acquires it.
_start_lock: asyncio.Lock | None = None
_start_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_start_lock() -> asyncio.Lock:
    global _start_lock, _start_lock_loop
    loop = asyncio.get_running_loop()
    if _start_lock is None or _start_lock_loop is not loop:
        _start_lock = asyncio.Lock()
        _start_lock_loop = loop
    return _start_lock


async def start_producer() -> None:
    """Start the producer, once at app startup. The global is assigned only *after* ``start()``
    succeeds, or the lazy restart below can never fire."""
    global _producer
    if _producer is not None:
        return
    settings = get_settings()
    producer = AIOKafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        value_serializer=lambda v: json.dumps(v).encode(),
        key_serializer=lambda k: k.encode() if isinstance(k, str) else k,
        # Wait for all in-sync replicas to acknowledge — strongest durability guarantee
        acks="all",
        # Broker-side dedup of producer retries (needs acks="all"). ONLY those — app-level
        # duplicates are made safe by `JobRepository.claim_for_running`.
        enable_idempotence=True,
        # Retry up to 5 times on transient errors
        retry_backoff_ms=200,
    )
    await producer.start()
    _producer = producer
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


async def _ensure_producer() -> AIOKafkaProducer:
    """Return the running producer, starting it if boot-time start failed — the self-heal.
    Throttled to one attempt per ``_START_RETRY_MIN_INTERVAL``; inside that window it raises."""
    global _last_start_attempt
    producer = _producer
    if producer is not None:
        return producer
    async with _get_start_lock():
        if _producer is not None:
            return _producer
        if time.monotonic() - _last_start_attempt < _START_RETRY_MIN_INTERVAL:
            raise RuntimeError("Kafka producer not started; last start attempt failed recently")
        _last_start_attempt = time.monotonic()
        await start_producer()
        return _get_producer()


async def _publish(topic: str, key: str, payload: dict[str, Any]) -> None:
    """Send a single message; log and swallow errors so Kafka issues don't crash the API."""
    try:
        validate_schema(topic, payload)
        producer = await _ensure_producer()
        await producer.send_and_wait(topic, value=payload, key=key)
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
    """Send a message and propagate errors, so the outbox relay can retry the row — or fail it on
    `SchemaValidationError`."""
    # Validate before ensuring the producer: a schema bug must surface as
    # SchemaValidationError (so the relay fails the row) and must not burn one
    # of the throttled producer-start attempts.
    validate_schema(topic, payload)
    producer = await _ensure_producer()
    await producer.send_and_wait(topic, value=payload, key=key)


async def publish_job_progress(
    job_id: uuid.UUID,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    status: str,
    percent: int,
    message: str,
    retry_count: int = 0,
) -> None:
    """Publish progress straight to Kafka — the one event that skips the outbox, being
    disposable."""
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


