import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.models.base import Base, PortableJSON
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.job import Job
    from app.models.user import User

# Principal type discriminator on audit_logs.principal_type. Kept as string
# literals rather than an enum so the DB constraint stays lax — new principal
# shapes (Wave-3 approval bots, etc.) can be added without a schema change.
PRINCIPAL_TYPE_USER = "user"
PRINCIPAL_TYPE_SERVICE_ACCOUNT = "service_account"


class AuditLog(Base):
    """Immutable append-only record of every significant action in the system.

    Two principal shapes write here: humans (`principal_type='user'`,
    `principal_id` = users.id, `user_id` also set for backward compat with
    the FK-based path) and service accounts (`principal_type='service_account'`,
    `principal_id` = service_accounts.id, `user_id` null). Consumers filter
    on principal_type when they want to isolate operator activity from agent
    activity.
    """

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Kept for backward compat with existing queries; new machine-principal
    # rows leave it null. See `principal_type` + `principal_id` for the
    # canonical actor identity going forward.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    principal_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PRINCIPAL_TYPE_USER,
        server_default=PRINCIPAL_TYPE_USER,
    )
    # Plain UUID column, not an FK — points at users.id or service_accounts.id
    # depending on principal_type. Kept unconstrained so a deleted principal
    # doesn't cascade or block audit reads (the row is a historical record).
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    resource_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(50), nullable=True)
    extra_data: Mapped[dict[str, Any] | None] = mapped_column(PortableJSON, nullable=True)
    # Kafka coordinates — set ONLY by the AuditConsumer for event.* rows so
    # redelivery dedups on the unique constraint below. Nullable is
    # load-bearing: inline transactional audit writers (API routes, worker
    # _run_job, saga coordinator) have no Kafka coords and leave all three
    # NULL; both Postgres and SQLite treat NULLs as distinct under UNIQUE,
    # so inline rows never collide.
    kafka_topic: Mapped[str | None] = mapped_column(String(128), nullable=True)
    kafka_partition: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kafka_offset: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User | None"] = relationship(
        "User", back_populates="audit_logs", lazy="noload"
    )
    job: Mapped["Job | None"] = relationship(
        "Job", back_populates="audit_logs", lazy="noload"
    )

    __table_args__ = (
        # Kafka redelivery → second event.* write fails this constraint, the
        # AuditConsumer treats the IntegrityError as "already recorded" and
        # commits the offset. Mirrors uq_job_events_kafka_coord.
        UniqueConstraint(
            "kafka_topic", "kafka_partition", "kafka_offset", name="uq_audit_logs_kafka_coord"
        ),
    )

    def __repr__(self) -> str:
        return f"<AuditLog id={self.id} action={self.action}>"
