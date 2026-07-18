"""
Idempotency records for machine-principal actions.

Rows are keyed by (tenant, principal, key). Same tenant+principal+key
with matching `arguments_hash` returns the cached `response_json`;
mismatched hash refuses the request. See ADR-forthcoming; this is
standard idempotency-key semantics (Stripe-shape).
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
    # Plain UUID (no FK) — points at either users.id or
    # service_accounts.id depending on principal_type. Matches the
    # `audit_logs.principal_id` convention.
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(
        PortableJSON, nullable=False
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
