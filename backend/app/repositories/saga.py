"""Repository for sagas."""

import uuid

from app.models.enums import JobStatus
from app.models.job import Job
from app.models.saga import Saga
from app.repositories.base import BaseRepository
from sqlalchemy import select

# The order a saga's steps are read in: declaration order, then `(created_at, id)`
# for rows with no index. One expression, used by every query returning steps,
# because two of them disagreeing is the bug it prevents: `created_at` is
# `transaction_timestamp()`, so one saga's steps tie under it and a uuid tiebreaker
# gives stable *random* order. NULLS LAST keeps `.compensate` rows below their steps.
_STEP_ORDER = (
    Job.saga_step_index.asc().nulls_last(),
    Job.created_at.asc(),
    Job.id.asc(),
)


class SagaRepository(BaseRepository[Saga]):
    model = Saga

    async def get_for_tenant(
        self,
        saga_id: uuid.UUID,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
    ) -> Saga | None:
        """Tenant-scoped (and optionally owner-scoped) get_by_id.

        None for another tenant's or another user's saga; a saga is a user's if any
        of its jobs are.
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
        """All jobs belonging to a saga, in declaration order.

        The API returns this as a saga's `steps`, so `_STEP_ORDER` is contract.
        """
        stmt = select(Job).where(Job.saga_id == saga_id).order_by(*_STEP_ORDER)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def completed_steps(self, saga_id: uuid.UUID) -> list[Job]:
        """Completed steps of a saga in declaration order.

        Compensation is this list reversed, so the order is load-bearing: it is what
        makes "undo the most recent success first" true (`saga_step_index`, WO-R2-58).
        """
        stmt = (
            select(Job)
            .where(Job.saga_id == saga_id, Job.status == JobStatus.COMPLETED)
            .order_by(*_STEP_ORDER)
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
        """List sagas in one tenant; `user_id=None` means every saga in it.

        `tenant_id` is required, not optional: a privileged caller is privileged inside
        their tenant, not across the platform (WO-R2-50).
        """
        from sqlalchemy import func

        base = select(Saga).where(Saga.tenant_id == tenant_id)
        count_stmt = (
            select(func.count()).select_from(Saga).where(Saga.tenant_id == tenant_id)
        )
        if user_id is not None:
            # A saga "belongs to" a user if any of its jobs are theirs.
            sub = select(Job.saga_id).where(Job.user_id == user_id).distinct()
            base = base.where(Saga.id.in_(sub))
            count_stmt = count_stmt.where(Saga.id.in_(sub))

        result = await self.session.execute(
            base.order_by(Saga.created_at.desc(), Saga.id.desc())
            .offset(offset)
            .limit(limit)
        )
        total_result = await self.session.execute(count_stmt)
        return list(result.scalars().all()), int(total_result.scalar_one())
