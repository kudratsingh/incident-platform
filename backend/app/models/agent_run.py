"""
`agent_runs` — what an autonomous responder said it was doing, while it did it.

One row per run, written over MCP by the responder's own principal and read back only
by a human operator (ADR 0035). The platform stores the report and never interprets
it: `state`, `current_hypothesis` and `last_step` are the caller's words, and
`phase_history` is append-only so a console can draw a timeline that no later report
can rewrite. `briefing` lands once, at the end.

`id` is supplied by the caller (its own run id), so a repeat report is an update of a
row it already knows the key of rather than a search.
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


class AgentRun(Base):
    __tablename__ = "agent_runs"

    # The caller's own run id, not generated here: `report_agent_run` is an upsert by
    # it, so two reports of one run must land on one row without a lookup by content.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # The alert this run answers, when the caller names one. SET NULL rather than
    # CASCADE: the record of a run outlives the alert that started it.
    alert_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("alerts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # The writing principal. A real FK, unlike `audit_logs.principal_id`, because this
    # column can only ever name a service account (ADR 0007).
    service_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # A short stable name the caller uses for this run, for an operator reading the
    # console later. Free text: the platform has no list of valid names.
    scenario: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # An `AgentRunState` value. Plain string, as `jobs.status` is, so a new member
    # needs no DDL.
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # Append-only list of `{"state": ..., "at": ...}`, oldest first, one entry per
    # state CHANGE. A repeat report of the same state adds nothing, so the list is a
    # timeline rather than a call log.
    phase_history: Mapped[list[dict[str, Any]]] = mapped_column(
        PortableJSON, nullable=False, default=list
    )
    # `{"name": ..., "category": ..., "confidence": ...}` or NULL while the caller has
    # no leading explanation. Replaced wholesale on every report.
    current_hypothesis: Mapped[dict[str, Any] | None] = mapped_column(
        PortableJSON, nullable=True
    )
    # `{"kind": "read"|"action", "tool": ..., "at": ...}` or NULL before the first step.
    last_step: Mapped[dict[str, Any] | None] = mapped_column(
        PortableJSON, nullable=True
    )
    # The caller's own end-of-run write-up, plus `prose` when it wrote one. Set once:
    # a second write is refused rather than merged, so what an operator read cannot
    # change under them.
    briefing: Mapped[dict[str, Any] | None] = mapped_column(
        PortableJSON, nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    # Stamped when `state` first reaches a terminal member, and never cleared.
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", lazy="noload")

    def __repr__(self) -> str:
        return f"<AgentRun id={self.id} state={self.state}>"
