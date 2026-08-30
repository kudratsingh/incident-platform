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


def _default_max_retries(_ctx: Any = None) -> int:
    """Retry ceiling for a row that does not name one, from `MAX_JOB_RETRIES`.

    A callable rather than a constant so SQLAlchemy resolves it per
    INSERT: the setting is read at flush time, which is what lets an
    operator change the ceiling by restarting with a new environment
    instead of by editing three literals (WO-R2-76).

    The import is deferred to keep `app.models` free of an import-time
    dependency on `app.config` — models are the one layer nothing else
    may have to import config *through*.
    """
    from app.config import get_settings

    return get_settings().max_job_retries


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
    # Caller-supplied key for idempotent creation — same key → same job returned.
    # Uniqueness is scoped per-tenant (composite constraint below) so different
    # tenants can reuse the same key without colliding.
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(PortableJSON, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(PortableJSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Resolved per INSERT from `MAX_JOB_RETRIES` rather than frozen at a
    # literal 3, so the documented knob governs rows written outside
    # `JobService` too — the chaos hooks, the eval seeds, saga steps
    # (WO-R2-76). See `_default_max_retries` for why it is a callable.
    max_retries: Mapped[int] = mapped_column(
        Integer, default=_default_max_retries, nullable=False
    )
    # Coarse categorization the agent uses to decide DLQ remediation:
    #   `replay_safe`      — transient / poison; replay after fix
    #   `wait_and_replay`  — external dep down; retry after recovery
    #   `human_required`   — persistent bug; do NOT replay
    # Set by the LLM triage service (Phase 10) when `LLM_TRIAGE_ENABLED`
    # is on — it is off by default, so on a stock deployment the only
    # writers are the seed script, the chaos hooks and `mark_dlq_permanent`
    # (R2-24). Nullable — only DLQ entries carry a value today, and NULL
    # reads as "not categorised", not as "safe to replay".
    # Cleared on replay (R2-23): the value describes one dead-letter
    # episode, not the job.
    # Kept as a plain string (no CHECK constraint) so new categories can
    # be added without a schema change.
    remediation_hint: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Which mechanism forced this job into the DLQ, when it was NOT the
    # default one. Today's sole value is `llm_retry_policy` — the LLM-guided
    # retry policy returning `dead_letter_now` while retries remained.
    # NULL = the default mechanism (retries exhausted, no registered
    # processor, or the dispatcher's safety net), so a NULL row renders
    # unbadged rather than being attributed to a policy that never ran.
    # A different axis from remediation_hint, which says what to do NEXT.
    dead_lettered_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
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
    # When the stale-PENDING backstop last re-published this job (WO-R2-28).
    # The backstop's own de-duplication marker: it stamps this inside the same
    # transaction as the outbox insert and then refuses to re-publish a job it
    # already re-published inside the cutoff window. Kept separate from
    # `updated_at` on purpose — that one is the staleness signal ("time since
    # last progress") and is rendered to operators, so a sweep write must not
    # be able to masquerade as progress or reset the visible age.
    requeued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When the worker executing this job last checked in (WO-R2-28). Renewed
    # by `_renew_running_leases_loop` while the job is this process's, read by
    # the stale-RUNNING sweep in every replica. NULL means nobody has checked
    # in — which is what a crash orphan looks like, so NULL reads as stale.
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    saga_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sagas.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Position of this step in its saga's declaration order, 0-based, written
    # once at creation (WO-R2-58). It exists because `created_at` cannot carry
    # this: `func.now()` is Postgres `transaction_timestamp()`, so every step
    # of a saga — all inserted by one request — shares an identical value and
    # `ORDER BY created_at` over them is a total tie. Compensation rolls back
    # in reverse of this column, and "undo the most recent success first" is a
    # correctness property, not a display preference.
    #
    # NULL for everything that is not a declared step: ordinary jobs, and the
    # `.compensate` rows the coordinator mints (they are ordered by the steps
    # they undo, not by a position of their own).
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
