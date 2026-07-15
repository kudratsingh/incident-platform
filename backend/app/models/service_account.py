"""
Service accounts — first-class machine principals.

Rationale in `docs/ADR/0007-machine-principal-scope-model.md`. Two tables:

- `service_accounts` is the durable principal. Tenant-scoped, named, carries
  the maximum scope set for its tokens, and has an active flag as a soft
  kill switch.
- `service_account_tokens` holds SHA-256 hashes of the actual bearer tokens.
  Multiple active tokens per account (supports rotation without disruption),
  each with its own scope subset, expiry, and revocation timestamp. The
  plaintext token is shown to the operator exactly once — at mint time.
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
        # Names must be unique within a tenant so operators can refer to
        # accounts by name in tooling; global uniqueness would be too
        # restrictive across tenants.
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
    # The maximum scope set for this account. Minted tokens must carry a
    # subset of this list; enforcement lives in the token minting service.
    # Stored as JSON for schema simplicity — the scope enum is fixed and
    # small, and we filter on membership rather than JSON operators.
    scopes: Mapped[list[str]] = mapped_column(PortableJSON, nullable=False, default=list)
    # Soft kill switch. Setting False disables *all* tokens for this account
    # without touching them individually; useful for the Wave 3 per-principal
    # kill switch.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Nullable because the seed principal (`incident-commander`) is created
    # by a migration, not a human user. SET NULL on user deletion so we don't
    # cascade-drop service accounts.
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

    The plaintext is never persisted — it's returned to the operator at mint
    time and thrown away. Verification hashes the presented token and looks
    up the row; a hit means the token is valid *if* it isn't revoked or
    expired.
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
    # SHA-256 of the plaintext bearer token. Unique so the lookup is a
    # single-row hit; index on this column is the hot path on every request
    # authenticated with an sa_ token.
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
    # Refreshed on every successful auth; useful for operators to spot
    # tokens that are stale and safe to revoke. Best-effort — not on the
    # request's hot path (async best-effort update).
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
