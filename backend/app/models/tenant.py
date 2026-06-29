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
from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.user import User


DEFAULT_TENANT_SLUG = "default"
# Matches the UUID seeded by the f8a1c4e23507 migration. Models use this as
# the column-level default for tenant_id so every existing insert site keeps
# working without code change — they implicitly write to the default tenant.
# PR B (Phase 12 enforcement) replaces these defaults with explicit
# propagation from the authenticated request.
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
