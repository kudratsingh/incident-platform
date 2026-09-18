"""
Alerts — durable outbound signals about platform state.

Reached by an HMAC-signed webhook on create (`Settings.alert_webhook_url`) or by the
`list_active_alerts` MCP tool. No state machine, just `fired_at` / `resolved_at`.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.models.base import Base, PortableJSON
from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.tenant import Tenant

# The vocabulary a producer may assert. `low` was added by WO-R2-124 so the
# commander's noise branches are reachable from a real alert. Deliberately four
# values: `medium`/`high` and `unknown` were declined (ADR 0025).
SEVERITY_LOW = "low"
SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

ALLOWED_SEVERITIES = frozenset(
    {SEVERITY_LOW, SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_CRITICAL}
)


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
    # Freeform source string — `slo:job_completion`, `dlq:threshold`.
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
    # De-duplication identity for producers that fire on one sustained condition
    # (WO-R2-29); NULL when not needed, and NULLs do not collide below. The constraint
    # is what makes it safe: `worker_loop` runs in every replica, so a check-then-insert
    # would let both alert — hence the window lives in `_fast_burn_dedup_key`.
    dedup_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    tenant: Mapped["Tenant"] = relationship("Tenant", lazy="noload")

    __table_args__ = (
        UniqueConstraint("tenant_id", "dedup_key", name="uq_alerts_tenant_dedup_key"),
    )

    def __repr__(self) -> str:
        return f"<Alert id={self.id} severity={self.severity} source={self.source}>"
