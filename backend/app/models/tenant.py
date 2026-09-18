"""
Tenants — the unit of isolation in the multi-tenant model.

Every tenant-scoped row carries `tenant_id`; queries are scoped at the repository
layer with RLS underneath. `slug` is the URL-safe identifier, `name` the display
string; `is_active=False` suspends without deleting. The initial migration creates
one bootstrap tenant, slug=`default`.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from app.models.base import Base
from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.user import User


DEFAULT_TENANT_SLUG = "default"
# Matches the UUID seeded by the f8a1c4e23507 migration. No model declares it as a
# column default: an insert site that forgets a tenant must fail loudly rather than
# write to the default tenant (the one explicit use left is the platform-owned SLO
# fast-burn alert in `app/services/slo.py`, which has no request context). The hex is
# deliberately mixed letters/digits — SQLite stores an integer-looking UUID as an
# integer, which then fails to round-trip.
DEFAULT_TENANT_ID = uuid.UUID("d3fa17de-7a17-de7a-17de-7a17de7a17de")


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 0 disables the check; this is an enforcement mechanism, not a billing knob.
    rate_limit_per_minute: Mapped[int] = mapped_column(
        Integer, nullable=False, default=120, server_default="120"
    )
    quota_jobs_per_month: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100_000, server_default="100000"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    users: Mapped[list["User"]] = relationship(
        "User", back_populates="tenant", lazy="noload"
    )

    def __repr__(self) -> str:
        return f"<Tenant id={self.id} slug={self.slug}>"
