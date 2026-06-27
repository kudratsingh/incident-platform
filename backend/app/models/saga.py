"""
Saga — a multi-step distributed workflow whose steps are jobs.

Each step is an ordinary Job with `saga_id` set. The chain is wired up via
the job dependency DAG (each step depends on the previous), so the
generic DependencyResolver handles step-to-step transitions. SagaCoordinator
watches the lifecycle and updates the saga's own status, including
publishing compensation events when a step fails.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from app.models.base import Base
from app.models.enums import SagaStatus
from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.job import Job


class Saga(Base):
    __tablename__ = "sagas"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SagaStatus.RUNNING, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    jobs: Mapped[list["Job"]] = relationship(
        "Job", back_populates="saga", lazy="noload"
    )

    def __repr__(self) -> str:
        return f"<Saga id={self.id} name={self.name} status={self.status}>"
