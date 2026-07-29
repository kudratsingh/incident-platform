"""
Deploy markers — one row per deploy landed on this environment.

Populated today by `scripts/seed_eval_fixtures.py` for eval scenarios;
future work wires the release pipeline (release.yml on tag push) to
insert a row on every successful deploy. `get_deploy_history` reads
the most recent N rows, falling back to env vars when empty.

The `tenant_id` column is nullable because deploys are platform-wide
today. It exists so a future per-tenant deploy story has somewhere
to hang without a schema change.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.models.base import Base, PortableJSON
from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

if TYPE_CHECKING:
    pass


class DeployMarker(Base):
    __tablename__ = "deploy_markers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Nullable — deploys are platform-wide, not tenant-scoped.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=True,
    )
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image_tag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    deployed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    extra_data: Mapped[dict[str, Any] | None] = mapped_column(
        PortableJSON, nullable=True
    )

    def __repr__(self) -> str:
        return f"<DeployMarker version={self.version} env={self.environment}>"
