"""
Outbox table for the transactional outbox pattern.

A row is inserted in the same transaction as the state change it describes; a
background relay publishes it to Kafka — at-least-once delivery across a crash
between commit and publish. A row that can never publish is dead-lettered
(`failed_at` / `error_message`, ADR 0001), freeing its slot in the fetch window.
"""

import uuid
from datetime import datetime
from typing import Any

from app.models.base import Base, PortableJSON
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    # Partition key — usually the user_id so per-user ordering is preserved.
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: Set when the relay is *done* with this row, published or dead-lettered — the
    #: "leaves the window" marker, not proof of delivery. Read with `failed_at`.
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Non-NULL exactly when this row was abandoned without reaching Kafka.
    #: `published_at IS NOT NULL AND failed_at IS NULL` is a real publish.
    failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Why it was abandoned; `WHERE failed_at IS NOT NULL` is the dead-letter queue.
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Hot path: the relay only ever scans unpublished rows. A partial index
        # keeps it tiny even as the table accumulates published history.
        Index(
            "ix_outbox_events_unpublished",
            "created_at",
            postgresql_where="published_at IS NULL",
        ),
    )

    def __repr__(self) -> str:
        if self.failed_at is not None:
            return f"<OutboxEvent id={self.id} topic={self.topic} FAILED>"
        published = self.published_at is not None
        return f"<OutboxEvent id={self.id} topic={self.topic} published={published}>"
