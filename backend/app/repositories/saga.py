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

    async def list_for_user(
        self,
        user_id: uuid.UUID | None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Saga], int]:
        """List sagas. user_id=None means 'all sagas' (admin view)."""
        from sqlalchemy import func

        base = select(Saga)
        count_stmt = select(func.count()).select_from(Saga)
        if user_id is not None:
            # A saga "belongs to" a user if any of its jobs are theirs. Jobs are
            # always created by one user for the saga's lifetime, so a subquery
            # over jobs.user_id == user_id is sufficient.
            sub = select(Job.saga_id).where(Job.user_id == user_id).distinct()
            base = base.where(Saga.id.in_(sub))
            count_stmt = count_stmt.where(Saga.id.in_(sub))

        result = await self.session.execute(
            base.order_by(Saga.created_at.desc()).offset(offset).limit(limit)
        )
        total_result = await self.session.execute(count_stmt)
        return list(result.scalars().all()), int(total_result.scalar_one())
