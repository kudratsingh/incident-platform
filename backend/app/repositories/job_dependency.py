"""Repository for the job_dependencies many-to-many self-join."""

import uuid

from app.models.enums import JobStatus
from app.models.job import Job
from app.models.job_dependency import JobDependency
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


class JobDependencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, job_id: uuid.UUID, depends_on_ids: list[uuid.UUID]) -> None:
        """Insert dependency rows. Caller is responsible for the surrounding tx."""
        for parent in depends_on_ids:
            self.session.add(JobDependency(job_id=job_id, depends_on_job_id=parent))
        await self.session.flush()

    async def parents(self, job_id: uuid.UUID) -> list[uuid.UUID]:
        stmt = select(JobDependency.depends_on_job_id).where(
            JobDependency.job_id == job_id
        )
        result = await self.session.execute(stmt)
        return [r[0] for r in result.all()]

    async def children_of(self, parent_id: uuid.UUID) -> list[uuid.UUID]:
        """Job ids that have `parent_id` as one of their dependencies."""
        stmt = select(JobDependency.job_id).where(
            JobDependency.depends_on_job_id == parent_id
        )
        result = await self.session.execute(stmt)
        return [r[0] for r in result.all()]

    async def unmet_count(self, job_id: uuid.UUID) -> int:
        """How many parents are not yet COMPLETED."""
        stmt = (
            select(func.count())
            .select_from(JobDependency)
            .join(Job, Job.id == JobDependency.depends_on_job_id)
            .where(
                JobDependency.job_id == job_id,
                Job.status != JobStatus.COMPLETED,
            )
        )
        result = await self.session.execute(stmt)
        return int(result.scalar_one())
