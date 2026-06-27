"""
job_events — immutable append-only log of lifecycle events, written by the
EventLogConsumer from the Kafka topics. This is the event-sourcing store:
given a job_id, the full state history can be reconstructed by replaying
its events in offset order.

Idempotency: a UNIQUE constraint on (kafka_topic, kafka_partition,
kafka_offset) makes Kafka redelivery a no-op (the second write fails the
constraint and the consumer treats it as success).
"""

import uuid
from datetime import datetime
from typing import Any

from app.models.base import Base, PortableJSON
from sqlalchemy import BigInteger, DateTime, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    event_name: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False)

    kafka_topic: Mapped[str] = mapped_column(String(128), nullable=False)
    kafka_partition: Mapped[int] = mapped_column(Integer, nullable=False)
    kafka_offset: Mapped[int] = mapped_column(BigInteger, nullable=False)

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Kafka redelivery → second write fails this constraint, consumer treats
        # the IntegrityError as "already recorded" and commits the offset.
        UniqueConstraint(
            "kafka_topic", "kafka_partition", "kafka_offset", name="uq_job_events_kafka_coord"
        ),
        # Hot path: timeline endpoint orders by recorded_at (offset order within
        # a single partition is preserved by recorded_at because the consumer
        # processes serially per partition).
        Index("ix_job_events_job_recorded", "job_id", "recorded_at"),
    )

    def __repr__(self) -> str:
        return f"<JobEvent id={self.id} event={self.event_name} job_id={self.job_id}>"
