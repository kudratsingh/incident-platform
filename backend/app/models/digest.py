"""
Periodic incident summaries.

One row per (tenant, window) that the digest worker has summarized. The
worker queries the event log + jobs table for the window, hands a small
aggregate to Claude, and persists the narrative + structured highlights
here. The admin UI lists these in reverse-chronological order.

Why a separate table rather than recomputing each request: the digest
is expensive to generate (LLM call, multi-second latency, real cost)
and exactly the same answer is asked for repeatedly by every admin
viewing the tab. Persisting once + serving many is the right trade.
"""

import uuid
from datetime import datetime
from typing import Any

from app.models.base import Base, PortableJSON
from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


class IncidentDigest(Base):
    __tablename__ = "incident_summaries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    summary: Mapped[str] = mapped_column(Text, nullable=False)
    # The LLM's structured highlights — top concerns, recommended actions,
    # and the raw stats we fed in. Stored as one JSONB blob so we can
    # evolve the shape without migrations.
    highlights: Mapped[dict[str, Any] | None] = mapped_column(
        PortableJSON, nullable=True
    )

    model_used: Mapped[str] = mapped_column(String(64), nullable=False)
    usage: Mapped[dict[str, Any] | None] = mapped_column(PortableJSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<IncidentDigest tenant={self.tenant_id} "
            f"window=[{self.window_start}..{self.window_end}]>"
        )
