"""
Outbox table for the transactional outbox pattern.

A row is inserted in the same DB transaction as the state change it describes
(job created, job completed, ...). A background relay polls unpublished rows
and publishes them to Kafka, marking each row as published only on success.

This guarantees at-least-once delivery: if the API crashes between the DB
commit and the Kafka publish, the row sits in the outbox until the next
relay tick.

"Until the next relay tick" is bounded, not infinite. A row that can never
publish (schema-invalid payload, oversize record) is dead-lettered — see
`failed_at` / `error_message` below and ADR 0001's 2026 Q3 addendum — so it
stops occupying a slot in the relay's fixed oldest-N fetch window.
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
    #: Set when the relay is *done* with this row — either because the
    #: publish succeeded, or because the row was dead-lettered. It is the
    #: "leaves the fetch window" marker, not proof of delivery: read it
    #: together with `failed_at` (ADR 0001, Decision item 3, which specifies
    #: `published_at=NOW, error_message=...` for the failed case).
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Non-NULL exactly when this row was abandoned without reaching Kafka.
    #: `published_at IS NOT NULL AND failed_at IS NULL` is a real publish.
    failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Why it was abandoned — the exception that finally settled it. This is
    #: what on-call reads; `SELECT topic, error_message, attempts FROM
    #: outbox_events WHERE failed_at IS NOT NULL` is the dead-letter queue.
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
