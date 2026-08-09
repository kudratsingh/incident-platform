"""
Base Kafka consumer — handles connection lifecycle, offset management, and
graceful shutdown. Subclasses implement handle_message() for their specific logic.

Offset management strategy:
  - After each successful handle_message(), the consumer commits
    {TopicPartition: message.offset + 1} — exactly that message's partition.
    The argument-less commit() form is never used: it would snapshot every
    assigned partition's fetch position, committing past unprocessed messages.
  - If handle_message() raises, nothing is committed; the consumer seeks back
    to the failed offset and abandons the rest of that partition's batch, so
    the NEXT POLL redelivers the message (at-least-once delivery). Other
    partitions keep processing.
  - Schema-invalid messages are poison pills: committed past, per-partition,
    so they can never stall their partition.
  - Duplicate deliveries are made safe by the dispatcher's atomic
    PENDING->RUNNING claim (`JobRepository.claim_for_running`) — idempotency
    keys only dedupe job CREATION, not execution.
"""

import asyncio
import json
from abc import ABC, abstractmethod
from typing import Any

from aiokafka import (  # type: ignore[import-untyped]
    AIOKafkaConsumer,
    ConsumerRecord,
    TopicPartition,
)
from app.config import get_settings
from app.core.logging import get_logger
from app.workers.schema_registry import SchemaValidationError
from app.workers.schema_registry import validate as validate_schema

logger = get_logger(__name__)


def kill_key_for(group_id: str) -> str:
    """Redis key the chaos `kill_consumer` tool sets to shut a specific
    consumer group down. Consumers check this key at the top of every
    poll iteration."""
    return f"chaos:kill:{group_id}"


def latency_key_for(group_id: str) -> str:
    """Redis key the chaos `inject_latency` tool sets. Value is an int
    number of milliseconds to sleep before each poll iteration."""
    return f"chaos:latency:{group_id}"


async def _check_chaos_kill(group_id: str) -> bool:
    """Return True when a `chaos:kill:<group>` key exists in Redis.

    Best-effort — Redis unavailable → return False. We never want the
    chaos check to block real message processing on a Redis blip.
    Import is deferred so plain-Kafka test paths don't spin up a real
    Redis client."""
    try:
        from app.core.redis import get_redis_client

        client = get_redis_client()
        val = await client.get(kill_key_for(group_id))
        return val is not None
    except Exception:
        return False


async def _check_chaos_kill_strict(group_id: str) -> bool:
    """Return True when a `chaos:kill:<group>` key exists in Redis, and RAISE
    when the lookup itself fails.

    Supervisor kill-window use: a lookup failure must read as still-killed,
    not as cleared — the poll-loop variant above deliberately fails open,
    this one deliberately does not. Treating an unknown kill state as
    "cleared" resurrects the consumer in the middle of the very window the
    chaos scenario is measuring.

    Trade-off: if Redis stays down for the whole window, the consumer stays
    down with it. Acceptable because the kill key carries a TTL — by the time
    Redis answers again the key has usually expired, so the restart proceeds.
    Import is deferred for the same reason as above."""
    from app.core.redis import get_redis_client

    client = get_redis_client()
    val = await client.get(kill_key_for(group_id))
    return val is not None


async def _check_chaos_latency(group_id: str) -> int:
    """Return the sleep duration in milliseconds from the chaos latency
    key, or 0 when unset / on any error. Best-effort like the kill
    check."""
    try:
        from app.core.redis import get_redis_client

        client = get_redis_client()
        val = await client.get(latency_key_for(group_id))
        if val is None:
            return 0
        return int(val)
    except Exception:
        return 0


class BaseKafkaConsumer(ABC):
    """
    Base class for all Kafka consumers in this application.

    Usage:
        class MyConsumer(BaseKafkaConsumer):
            async def handle_message(self, topic: str, key: str | None, value: dict) -> None:
                ...

        consumer = MyConsumer(topics=["job.submitted"], group_id="my-group")
        await consumer.start()          # in app startup
        asyncio.create_task(consumer.run())
        await consumer.stop()           # in app shutdown
    """

    def __init__(self, topics: list[str], group_id: str) -> None:
        self.topics = topics
        self.group_id = group_id
        self._consumer: AIOKafkaConsumer | None = None
        self._running = False
        self._chaos_killed = False

    async def start(self) -> None:
        settings = get_settings()
        self._consumer = AIOKafkaConsumer(
            *self.topics,
            bootstrap_servers=settings.kafka_bootstrap_servers,
            group_id=self.group_id,
            value_deserializer=lambda v: json.loads(v.decode()),
            key_deserializer=lambda k: k.decode() if k else None,
            # Commit offsets manually after successful processing
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            max_poll_interval_ms=settings.kafka_max_poll_interval_ms,
            session_timeout_ms=settings.kafka_session_timeout_ms,
        )
        await self._consumer.start()
        self._running = True
        self._chaos_killed = False
        logger.info(
            "kafka consumer started",
            extra={"group_id": self.group_id, "topics": self.topics},
        )

    async def stop(self) -> None:
        self._running = False
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None
        logger.info("kafka consumer stopped", extra={"group_id": self.group_id})

    @property
    def is_running(self) -> bool:
        """True between start() and stop()."""
        return self._running

    @property
    def chaos_killed(self) -> bool:
        """True when run() exited because the chaos kill key appeared."""
        return self._chaos_killed

    async def run(self) -> None:
        """
        Main consume loop. Runs until stop() is called.
        Commits each message's offset (per partition) only after successful
        handle_message(); failed messages are seeked back for redelivery.
        """
        if self._consumer is None:
            raise RuntimeError("Consumer not started — call start() first")

        while self._running:
            # Chaos framework kill switch (ADR 0008). If the
            # `kill_consumer` MCP tool has flipped the group's kill key,
            # exit the loop cleanly; the worker's supervisor decides
            # whether to restart. Only active when CHAOS_ENABLED=true;
            # otherwise the check short-circuits without a Redis call.
            if get_settings().chaos_enabled:
                if await _check_chaos_kill(self.group_id):
                    self._chaos_killed = True
                    logger.warning(
                        "kafka consumer stopped by chaos kill_consumer",
                        extra={"group_id": self.group_id},
                    )
                    break
                # Injected latency — sleep the requested milliseconds
                # before each poll. Capped inside the tool at 60_000 ms.
                latency_ms = await _check_chaos_latency(self.group_id)
                if latency_ms > 0:
                    await asyncio.sleep(latency_ms / 1000.0)

            try:
                # getmany() batches up to 10 messages per poll for efficiency
                records: dict[TopicPartition, list[ConsumerRecord]] = await asyncio.wait_for(
                    self._consumer.getmany(timeout_ms=500, max_records=10),
                    timeout=2.0,
                )
                had_failure = await self._process_batch(records)
                if had_failure:
                    # Pace the hot retry of a persistently failing message
                    # (it is refetched on the very next poll after seek-back);
                    # matches the loop-error backoff below.
                    await asyncio.sleep(1.0)

            except TimeoutError:
                # No messages — just loop again
                continue
            except asyncio.CancelledError:
                logger.info("kafka consumer loop cancelled", extra={"group_id": self.group_id})
                break
            except Exception as exc:
                logger.error(
                    "kafka consumer loop error",
                    extra={"group_id": self.group_id, "error": str(exc)},
                )
                await asyncio.sleep(1.0)

    async def _process_batch(self, records: dict[TopicPartition, list[ConsumerRecord]]) -> bool:
        """Process one getmany() batch; return True when any partition failed.

        Messages are processed in order within each partition. On the first
        failure the consumer seeks back to the failed offset and abandons the
        REST of that partition's batch — aiokafka discards its buffered
        records for a partition on seek and refetches from the seek offset,
        so the next poll redelivers from the failure point. Other partitions
        keep processing: a persistently failing message head-of-line-blocks
        only its own partition.
        """
        consumer = self._consumer
        if consumer is None:
            raise RuntimeError("Consumer not started — call start() first")

        had_failure = False
        for tp, messages in records.items():
            for message in messages:
                ok = await self._process_one(message)
                if not ok:
                    had_failure = True
                    try:
                        # seek() is synchronous in aiokafka — do not await.
                        consumer.seek(tp, message.offset)
                    except Exception as exc:
                        # A rebalance can deassign the partition between the
                        # failure and the seek (IllegalStateError). The new
                        # assignee resumes from the last committed offset,
                        # which is at or before this message — nothing lost.
                        logger.warning(
                            "seek-back failed — partition likely reassigned; "
                            "redelivery falls to committed offset",
                            extra={
                                "group_id": self.group_id,
                                "topic": tp.topic,
                                "partition": tp.partition,
                                "offset": message.offset,
                                "error": str(exc),
                            },
                        )
                    break  # abandon the rest of THIS partition's batch only
        return had_failure

    async def _process_one(self, message: ConsumerRecord) -> bool:
        """Process a single message, committing its partition's offset on success.

        Returns True when the partition may advance past this message (handled
        successfully, or a poison pill committed past); False when the message
        must be redelivered — the caller seeks back to this offset.
        """
        value: dict[str, Any] = message.value
        key: str | None = message.key
        tp = TopicPartition(message.topic, message.partition)

        # Schema validation — bad messages are a poison pill: re-delivering them
        # would block the partition forever. Commit past it (this partition
        # only) and move on; the producer side never should have sent this.
        try:
            validate_schema(message.topic, value)
        except SchemaValidationError as exc:
            logger.error(
                "kafka message dropped — schema invalid",
                extra={
                    "group_id": self.group_id,
                    "topic": message.topic,
                    "partition": message.partition,
                    "offset": message.offset,
                    "error": str(exc),
                },
            )
            await self._consumer.commit({tp: message.offset + 1})  # type: ignore[union-attr]
            return True

        try:
            await self.handle_message(
                message.topic,
                key,
                value,
                partition=message.partition,
                offset=message.offset,
            )
            # Commit this message's offset, for exactly this partition —
            # at-least-once delivery.
            await self._consumer.commit({tp: message.offset + 1})  # type: ignore[union-attr]
            return True
        except Exception as exc:
            # A failed commit (e.g. CommitFailedError on rebalance) lands here
            # too: report failure so the caller seeks back — reprocessing is
            # safe because handlers are idempotent under redelivery.
            logger.error(
                "kafka message handler failed — offset not committed, seeking back for redelivery",
                extra={
                    "group_id": self.group_id,
                    "topic": message.topic,
                    "partition": message.partition,
                    "offset": message.offset,
                    "error": str(exc),
                },
            )
            return False

    @abstractmethod
    async def handle_message(
        self,
        topic: str,
        key: str | None,
        value: dict[str, Any],
        *,
        partition: int = 0,
        offset: int = 0,
    ) -> None:
        """Implement this in each subclass to handle a single message.

        partition and offset are passed as keyword-only args so consumers that
        don't need Kafka coordinates can ignore them with `**_`. Required by
        EventLogConsumer for at-least-once dedup."""
        ...
