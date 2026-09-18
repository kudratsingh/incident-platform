"""
Periodic incident summaries — one row per (tenant, window) the digest worker has
summarized, listed newest-first in the admin UI.

A table rather than a recompute because the digest costs an LLM call and the same
answer is asked for repeatedly.
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
    # Structured highlights, one JSONB blob so the shape can evolve migration-free.
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
