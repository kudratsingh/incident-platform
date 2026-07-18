"""
Alerts — outbound signals about platform state that agents (or humans)
should know about.

Every alert is a durable row in this table. Consumers reach them one
of two ways:
  - Push: an HMAC-signed webhook fires on create, if
    `Settings.alert_webhook_url` is configured.
  - Poll: the `list_active_alerts` MCP tool reads unresolved rows.

Alerts don't have a state machine — just `fired_at` and `resolved_at`.
Coarse severity (`info` / `warning` / `critical`) plus a free-form
`source` string keep this useful without over-modeling.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.models.base import Base, PortableJSON
from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.tenant import Tenant

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

ALLOWED_SEVERITIES = frozenset({SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_CRITICAL})


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # Human-authored source string — `slo:job_completion`, `dlq:threshold`,
    # `chaos:manual`. Kept freeform because the set of alert producers
    # grows over time; a strict enum would hurt more than help.
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    fired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Null while active. Setting resolved_at removes the alert from the
    # `list_active_alerts` result set.
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    extra_data: Mapped[dict[str, Any] | None] = mapped_column(
        PortableJSON, nullable=True
    )
    request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    tenant: Mapped["Tenant"] = relationship("Tenant", lazy="noload")

    def __repr__(self) -> str:
        return f"<Alert id={self.id} severity={self.severity} source={self.source}>"
