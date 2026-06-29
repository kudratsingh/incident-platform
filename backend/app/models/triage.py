"""
LLM-driven triage analysis for dead-lettered jobs.

One row per terminally-failed job: the LLM consumer classifies the failure
root cause, summarises what happened, suggests a fix, and tells us whether
a retry is likely to succeed. Admins see this on the DLQ tab so they can
decide between Replay vs Resolve without having to read the raw stack
trace themselves.
"""

import uuid
from datetime import datetime
from typing import Any

from app.models.base import Base, PortableJSON
from sqlalchemy import DateTime, Float, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


class JobTriage(Base):
    __tablename__ = "job_triages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Unique → one triage per job. Re-running the consumer for the same job
    # (e.g. Kafka redelivery) is a no-op via ON CONFLICT.
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )

    root_cause_category: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_fix: Mapped[str] = mapped_column(Text, nullable=False)
    is_retryable: Mapped[bool] = mapped_column(nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    model_used: Mapped[str] = mapped_column(String(64), nullable=False)
    # Anthropic usage block for cost tracking + cache-hit visibility.
    usage: Mapped[dict[str, Any] | None] = mapped_column(PortableJSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<JobTriage job_id={self.job_id} category={self.root_cause_category}>"
