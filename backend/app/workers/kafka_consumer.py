"""
Base Kafka consumer — connection lifecycle, offsets, graceful shutdown; subclasses implement
`handle_message()`. Each success commits `{TopicPartition: offset + 1}` for that partition alone,
never the argument-less `commit()`, which would commit past unprocessed messages. A raising handler
commits nothing and seeks back, so the next poll redelivers (at-least-once, made safe by
`JobRepository.claim_for_running`). Schema-invalid messages are committed past as poison pills.
"""

import asyncio
import json
import math
import time
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


# The value the kill flag carries. Shared so the chaos hook and the sticky re-arm below cannot
# drift apart — anything reading the key treats presence, not content, as the signal.
KILL_FLAG_VALUE = "killed"


def kill_key_for(group_id: str) -> str:
    """Redis key the chaos `kill_consumer` tool sets to shut a specific
    consumer group down. Consumers check this key at the top of every
    poll iteration."""
    return f"chaos:kill:{group_id}"


def sticky_kill_key_for(group_id: str) -> str:
    """Redis key `kill_consumer(sticky=true)` sets beside the kill flag.
    Its value is the absolute unix deadline the flag may be re-armed until
    (ADR 0032); its own TTL is the same instant on Redis's clock."""
    return f"chaos:kill_sticky:{group_id}"


def latency_key_for(group_id: str) -> str:
    """Redis key the chaos `inject_latency` tool sets. Value is an int
    number of milliseconds to sleep before each poll iteration."""
    return f"chaos:latency:{group_id}"


def _sticky_deadline(raw: Any) -> float | None:
    """Parse a sticky marker into an absolute unix deadline, or None when unreadable.

    Unreadable fails OPEN — a corrupted value releases the group rather than wedging it for the
    life of the process."""
    if raw is None:
        return None
    try:
        deadline = float(raw)
    except (TypeError, ValueError):
        return None
    # NaN and inf parse as floats and would survive every comparison below.
    return deadline if math.isfinite(deadline) else None


async def _rearm_sticky_kill(client: Any, group_id: str, marker: Any) -> bool:
    """Put the kill flag back for what is LEFT of a sticky kill's window (ADR 0032).

    The window is absolute: the flag is re-armed with `pxat` at the deadline the hook stored, so
    restarting the group cannot extend it and the marker's own TTL ends it regardless. Writes only
    under `CHAOS_ENABLED` — the caller's read is unconditional (the strict check's fail-closed
    contract needs it), the write is gate 1 (ADR 0008).
    """
    deadline = _sticky_deadline(marker)
    if deadline is None or deadline - time.time() <= 0:
        return False
    if not get_settings().chaos_enabled:
        return False
    await client.set(
        kill_key_for(group_id), KILL_FLAG_VALUE, pxat=round(deadline * 1000)
    )
    return True


async def _read_kill_state(client: Any, group_id: str) -> bool:
    """True when this group is killed: the flag is set, or a sticky window re-arms it.

    One MGET for both keys, so the sticky option costs no extra round trip per poll."""
    marker_key = sticky_kill_key_for(group_id)
    flag, marker = await client.mget([kill_key_for(group_id), marker_key])
    if flag is not None:
        return True
    return await _rearm_sticky_kill(client, group_id, marker)


async def _check_chaos_kill(group_id: str) -> bool:
    """True when the group is killed (`chaos:kill:<group>`, or a sticky re-arm). Fails open, and
    defers the Redis import."""
    try:
        from app.core.redis import get_redis_client

        return await _read_kill_state(get_redis_client(), group_id)
    except Exception:
        return False


async def _check_chaos_kill_strict(group_id: str) -> bool:
    """Return True when the group is killed, and RAISE when the lookup fails.

    Fails CLOSED, unlike the variant above: for the supervisor's kill window an unknown state must
    not read as "cleared" and resurrect the consumer mid-measurement. A sticky window re-arms the
    flag here, which is what makes a Tier-1 restart genuinely fail; the deadline bounds it."""
    from app.core.redis import get_redis_client

    return await _read_kill_state(get_redis_client(), group_id)


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
    """Base class for every Kafka consumer here: `start()`, `run()` as a task, `stop()`."""

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
        """Main consume loop, until `stop()`. Commits per partition only on success."""
        if self._consumer is None:
            raise RuntimeError("Consumer not started — call start() first")

        while self._running:
            # Chaos kill switch (ADR 0008): exit cleanly on the kill key and let the supervisor
            # decide about a restart.
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

        In order within each partition. The first failure seeks back and abandons the REST of that
        partition's batch, so a persistently failing message blocks only its own partition.
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
                        # A rebalance can deassign the partition here; the new assignee resumes
                        # committed.
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
        """Process one message, committing its partition's offset on success. False means the
        caller must seek back to this offset for redelivery."""
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
        """Handle one message. `partition`/`offset` are keyword-only (`**_` to ignore); the event
        log needs them to dedup."""
        ...
