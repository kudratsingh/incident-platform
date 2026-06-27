"""Repository for sagas."""

import uuid

from app.models.enums import JobStatus
from app.models.job import Job
from app.models.saga import Saga
from app.repositories.base import BaseRepository
from sqlalchemy import select


class SagaRepository(BaseRepository[Saga]):
    model = Saga

    async def jobs(self, saga_id: uuid.UUID) -> list[Job]:
        """All jobs belonging to a saga, in creation order."""
        stmt = (
            select(Job)
            .where(Job.saga_id == saga_id)
            .order_by(Job.created_at.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def completed_steps(self, saga_id: uuid.UUID) -> list[Job]:
        stmt = (
            select(Job)
            .where(Job.saga_id == saga_id, Job.status == JobStatus.COMPLETED)
            .order_by(Job.created_at.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def waiting_steps(self, saga_id: uuid.UUID) -> list[Job]:
        stmt = select(Job).where(
            Job.saga_id == saga_id,
            Job.status.in_([JobStatus.WAITING, JobStatus.PENDING]),
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
