"""
Base Kafka consumer — handles connection lifecycle, offset management, and
graceful shutdown. Subclasses implement handle_message() for their specific logic.

Offset management strategy:
  - Offsets are committed ONLY after handle_message() returns successfully.
  - If handle_message() raises, the offset is NOT committed. On restart, the
    message will be re-delivered (at-least-once delivery).
  - Combined with idempotency keys on the job side, this prevents double execution.
"""

import asyncio
import json
from abc import ABC, abstractmethod
from typing import Any

from aiokafka import AIOKafkaConsumer, ConsumerRecord  # type: ignore[import-untyped]
from app.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


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

    async def run(self) -> None:
        """
        Main consume loop. Runs until stop() is called.
        Commits offset only after successful handle_message().
        """
        if self._consumer is None:
            raise RuntimeError("Consumer not started — call start() first")

        while self._running:
            try:
                # getmany() batches up to 10 messages per poll for efficiency
                records: dict[Any, list[ConsumerRecord]] = await asyncio.wait_for(
                    self._consumer.getmany(timeout_ms=500, max_records=10),
                    timeout=2.0,
                )
                for _tp, messages in records.items():
                    for message in messages:
                        await self._process_one(message)

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

    async def _process_one(self, message: ConsumerRecord) -> None:
        """Process a single message, committing offset on success."""
        try:
            value: dict[str, Any] = message.value
            key: str | None = message.key
            await self.handle_message(message.topic, key, value)
            # Commit after successful processing — at-least-once delivery
            await self._consumer.commit()  # type: ignore[union-attr]
        except Exception as exc:
            logger.error(
                "kafka message handler failed — offset not committed, will retry on restart",
                extra={
                    "group_id": self.group_id,
                    "topic": message.topic,
                    "partition": message.partition,
                    "offset": message.offset,
                    "error": str(exc),
                },
            )
            # Do NOT commit — message will be re-delivered on next startup

    @abstractmethod
    async def handle_message(
        self, topic: str, key: str | None, value: dict[str, Any]
    ) -> None:
        """Implement this in each subclass to handle a single message."""
        ...
