"""
Service accounts — first-class machine principals (ADR 0007).

`service_accounts` is the durable principal and carries the maximum scope set for
its tokens; `service_account_tokens` holds SHA-256 hashes, each with its own scope
subset and expiry. Plaintext is shown once, at mint time.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from app.models.base import Base, PortableJSON, TimestampMixin
from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.tenant import Tenant
    from app.models.user import User


class ServiceAccount(TimestampMixin, Base):
    __tablename__ = "service_accounts"
    __table_args__ = (
        # Unique per tenant so operators can name accounts; global is too strict.
        UniqueConstraint("tenant_id", "name", name="uq_service_accounts_tenant_name"),
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
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # The maximum scope set; minted tokens carry a subset, enforced in the minting
    # service. JSON because the enum is small.
    scopes: Mapped[list[str]] = mapped_column(PortableJSON, nullable=False, default=list)
    # Soft kill switch: False disables *all* tokens for this account at once.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Nullable: the seed principal comes from a migration, not a user.
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", lazy="noload")
    created_by: Mapped["User | None"] = relationship("User", lazy="noload")
    tokens: Mapped[list["ServiceAccountToken"]] = relationship(
        "ServiceAccountToken",
        back_populates="service_account",
        lazy="noload",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<ServiceAccount id={self.id} name={self.name} tenant={self.tenant_id}>"


class ServiceAccountToken(Base):
    """SHA-256 hash of a `sa_<random>` bearer token.

    The plaintext is never persisted; a hit is valid only if not revoked or expired.
    """

    __tablename__ = "service_account_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    service_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # SHA-256 of the plaintext token; this lookup is the hot path on every request.
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    # Subset of the account's scopes chosen at mint time. Supports
    # capability-narrowing without needing a new account.
    scopes: Mapped[list[str]] = mapped_column(PortableJSON, nullable=False, default=list)
    # Nullable = never expires. Default expiry is applied in the service
    # layer, not the schema, so the policy is easy to evolve.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Refreshed on every successful auth, best-effort and off the hot path.
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    service_account: Mapped["ServiceAccount"] = relationship(
        "ServiceAccount", back_populates="tokens", lazy="noload"
    )

    def __repr__(self) -> str:
        return f"<ServiceAccountToken id={self.id} sa={self.service_account_id}>"
