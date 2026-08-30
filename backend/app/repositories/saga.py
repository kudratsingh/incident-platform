"""Repository for sagas."""

import uuid

from app.models.enums import JobStatus
from app.models.job import Job
from app.models.saga import Saga
from app.repositories.base import BaseRepository
from sqlalchemy import select


class SagaRepository(BaseRepository[Saga]):
    model = Saga

    async def get_for_tenant(
        self,
        saga_id: uuid.UUID,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
    ) -> Saga | None:
        """Tenant-scoped (and optionally owner-scoped) get_by_id.

        Returns None when the saga belongs to another tenant, or — when
        `user_id` is given — to another user: never raises, never leaks the
        row. Same shape and same reason as `JobRepository.get_for_tenant`.
        Ownership follows `list_for_user`: a saga is a user's if any of its
        jobs are.
        """
        stmt = select(Saga).where(Saga.id == saga_id, Saga.tenant_id == tenant_id)
        if user_id is not None:
            stmt = stmt.where(
                Saga.id.in_(
                    select(Job.saga_id).where(Job.user_id == user_id).distinct()
                )
            )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

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
        tenant_id: uuid.UUID,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Saga], int]:
        """List sagas in one tenant. user_id=None means 'every saga in that
        tenant' (the admin/support view).

        `tenant_id` is required, not optional-with-a-default: the caller that
        wanted "all sagas" passed `user_id=None` and got every tenant's,
        with Postgres RLS the only thing between the response and a
        cross-tenant read (WO-R2-50). A privileged caller is privileged
        inside their tenant, not across the platform.
        """
        from sqlalchemy import func

        base = select(Saga).where(Saga.tenant_id == tenant_id)
        count_stmt = (
            select(func.count()).select_from(Saga).where(Saga.tenant_id == tenant_id)
        )
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
