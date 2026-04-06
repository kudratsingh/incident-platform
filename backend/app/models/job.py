import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.models.base import Base, PortableJSON, TimestampMixin
from app.models.enums import JobStatus
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.audit import AuditLog
    from app.models.user import User


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(50), default=JobStatus.PENDING, nullable=False, index=True
    )
    # Caller-supplied key for idempotent creation — same key → same job returned
    idempotency_key: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(PortableJSON, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(PortableJSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    # Higher number = higher priority in the queue
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    # Correlation ID from the originating HTTP request, for end-to-end tracing
    trace_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped["User"] = relationship("User", back_populates="jobs", lazy="noload")
    audit_logs: Mapped[list["AuditLog"]] = relationship(
        "AuditLog", back_populates="job", lazy="noload"
    )

    def __repr__(self) -> str:
        return f"<Job id={self.id} type={self.type} status={self.status}>"
