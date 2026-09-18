"""
Idempotency records for machine-principal actions, keyed by (tenant, principal,
key). A mismatched `arguments_hash` refuses. Lifecycle is ADR 0010.
"""

import uuid
from datetime import datetime
from typing import Any

from app.models.base import Base, PortableJSON
from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "principal_id",
            "idempotency_key",
            name="uq_idempotency_scope",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Plain UUID (no FK), as `audit_logs.principal_id` (ADR 0007).
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # NULL means "claimed, not yet answered": the key is reserved before the action
    # runs (R2-27), and both commit together.
    response_json: Mapped[dict[str, Any] | None] = mapped_column(
        PortableJSON, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return (
            f"<IdempotencyRecord tool={self.tool_name} key={self.idempotency_key}>"
        )
