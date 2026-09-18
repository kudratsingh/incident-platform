"""The `jobs` table — one row per unit of work.

Its `status` is the projection every other part of the platform reads: the
queue, the DLQ tools, the read model and the SLOs all speak about this row.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.models.base import Base, PortableJSON, TimestampMixin
from app.models.enums import JobStatus
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.audit import AuditLog
    from app.models.saga import Saga
    from app.models.user import User


def _default_max_attempts(_ctx: Any = None) -> int:
    """Run ceiling for a row that does not name one, from `MAX_JOB_ATTEMPTS`.

    A callable so SQLAlchemy resolves the setting per INSERT (WO-R2-76); the import
    is deferred to keep `app.models` free of an import-time `app.config` dependency.
    """
    from app.config import get_settings

    return get_settings().max_job_attempts


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(50), default=JobStatus.PENDING, nullable=False, index=True
    )
    # Caller-supplied idempotency key; uniqueness is per-tenant (constraint below).
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(PortableJSON, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(PortableJSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # How many times this job may RUN in total — original run plus retries, so 3 means
    # three runs and two retries (`max_retries` until WO-R2-172). Resolved per INSERT
    # from `MAX_JOB_ATTEMPTS`, so the knob governs rows outside `JobService` (WO-R2-76).
    max_attempts: Mapped[int] = mapped_column(
        Integer, default=_default_max_attempts, nullable=False
    )
    # Coarse DLQ category the agent routes on: `replay_safe` / `wait_and_replay` /
    # `human_required`. Writers are LLM triage (off by default), the seed script, the
    # chaos hooks and `mark_dlq_permanent` (R2-24). NULL means "not categorised", not
    # "safe to replay"; cleared on replay (R2-23). Plain string so new values need no DDL.
    remediation_hint: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Which mechanism forced this job into the DLQ, when not the default one.
    # Only value today is `llm_retry_policy`; NULL = the default mechanism, so a NULL
    # row renders unbadged. A different axis from remediation_hint (what to do NEXT).
    dead_lettered_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # When an operator last fenced this row with `mark_dlq_permanent`, and who
    # (WO-R2-158) — `remediation_hint` alone cannot say, so a fence was unobservable.
    # Re-stamped on EVERY mark, including a re-fence, in aware UTC: a different clock
    # from `completed_at` and `created_at`. Episode-scoped, so `replay_job` clears it
    # along with the hint (R2-23).
    fenced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The principal that raised the fence, as `"{principal_type}:{principal_id}"` —
    # self-describing because the id space is users.id or service_accounts.id
    # (ADR 0007). No FK: the record must survive the principal. NULL = never fenced.
    fenced_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Higher number = higher priority in the queue
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    # Correlation ID from the originating HTTP request, for end-to-end tracing
    trace_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When the stale-PENDING backstop last re-published this job (WO-R2-28) — its own
    # de-duplication marker, stamped in the same transaction as the outbox insert.
    # Separate from `updated_at`, which operators read as time since last progress.
    requeued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When the worker running this job last checked in (WO-R2-28); read by the
    # stale-RUNNING sweep in every replica, and NULL reads as stale.
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    saga_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sagas.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Position of this step in its saga's declaration order, 0-based (WO-R2-58).
    # `created_at` cannot carry it — `transaction_timestamp()` ties every step of one
    # request — and compensation rolls back in reverse of it. NULL for non-step rows.
    saga_step_index: Mapped[int | None] = mapped_column(Integer, nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="jobs", lazy="noload")
    saga: Mapped["Saga | None"] = relationship(
        "Saga", back_populates="jobs", lazy="noload"
    )
    audit_logs: Mapped[list["AuditLog"]] = relationship(
        "AuditLog", back_populates="job", lazy="noload"
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_jobs_tenant_idempotency_key"
        ),
    )

    def __repr__(self) -> str:
        return f"<Job id={self.id} type={self.type} status={self.status}>"
