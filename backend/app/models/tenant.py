"""
Tenants — the unit of isolation in the multi-tenant model.

Every tenant-scoped row in the database carries `tenant_id`. Queries are
scoped at the repository layer; in a later PR we'll layer Postgres row-level
security on top so the isolation is enforced at the DB even if a query
forgets to filter.

`slug` is the URL-safe identifier customers see (e.g. `acme`); `name` is
the display string. Both are unique. `is_active=False` disables a tenant
without deleting its data (suspended billing, offboarding flow).

There is one bootstrap tenant created in the initial migration with
slug=`default` — every pre-tenant row gets backfilled to it so existing
data stays accessible after the migration runs.
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
# Matches the UUID seeded by the f8a1c4e23507 migration.
#
# No model declares this as a column-level default: `tenant_id` is a
# required column everywhere and is propagated explicitly from the
# authenticated request. Column defaults were the pre-Phase-12 shape and
# were removed when enforcement landed, precisely so that an insert site
# which forgets a tenant fails loudly instead of silently writing to the
# default tenant. The one deliberate explicit use left is the platform-owned
# SLO fast-burn alert in `app/services/slo.py`, which has no request
# context to inherit a tenant from.
#
# Hex pattern is deliberately mixed letters/digits: SQLite has loose typing
# and stores a UUID whose 16 bytes happen to interpret as a small integer
# (e.g. 00000000-…-01) as the integer itself, which then fails to
# round-trip through the UUID column type. A pattern with non-trivial high
# bytes keeps it unambiguously a UUID at the storage layer.
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
    # 0 disables the rate limit / quota check for this tenant. The defaults
    # are intentionally generous — multi-tenancy is an enforcement mechanism
    # at the column level, not a billing knob; product decides real limits.
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
