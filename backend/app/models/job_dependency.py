"""
job_dependencies — many-to-many self-join on jobs.

A row (job_id, depends_on_job_id) means `job_id` cannot be dispatched until
`depends_on_job_id` reaches COMPLETED. The DependencyResolver consumer
watches job.completed events and unblocks waiting children.

Cycles are not possible at insert time because dependencies must reference
existing jobs and a new job has no children yet; the DAG only ever points
backward in time.
"""

import uuid

from app.models.base import Base
from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


class JobDependency(Base):
    __tablename__ = "job_dependencies"

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    depends_on_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )

    def __repr__(self) -> str:
        return f"<JobDependency job={self.job_id} depends_on={self.depends_on_job_id}>"
