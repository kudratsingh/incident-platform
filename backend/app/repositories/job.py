import uuid
from datetime import datetime
from typing import Any

from app.models.enums import JobStatus
from app.models.job import Job
from app.repositories.base import BaseRepository
from sqlalchemy import and_, func, select, update


class JobRepository(BaseRepository[Job]):
    model = Job

    async def get_by_idempotency_key(
        self, key: str, tenant_id: uuid.UUID
    ) -> Job | None:
        result = await self.session.execute(
            select(Job).where(
                Job.idempotency_key == key, Job.tenant_id == tenant_id
            )
        )
        return result.scalar_one_or_none()

    async def get_for_tenant(
        self, job_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> Job | None:
        """Tenant-scoped get_by_id. Returns None when the job belongs to a
        different tenant — never raises, never leaks the row."""
        result = await self.session.execute(
            select(Job).where(Job.id == job_id, Job.tenant_id == tenant_id)
        )
        return result.scalar_one_or_none()

    async def list_jobs(
        self,
        tenant_id: uuid.UUID,
        offset: int = 0,
        limit: int = 20,
        user_id: uuid.UUID | None = None,
        status: str | None = None,
        job_type: str | None = None,
        trace_id: str | None = None,
        created_after: Any = None,
        created_before: Any = None,
        retry_count_min: int | None = None,
        retry_count_max: int | None = None,
    ) -> tuple[list[Job], int]:
        filters: list[Any] = [Job.tenant_id == tenant_id]
        if user_id is not None:
            filters.append(Job.user_id == user_id)
        if status is not None:
            filters.append(Job.status == status)
        if job_type is not None:
            filters.append(Job.type == job_type)
        if trace_id is not None:
            filters.append(Job.trace_id == trace_id)
        if created_after is not None:
            filters.append(Job.created_at >= created_after)
        if created_before is not None:
            filters.append(Job.created_at <= created_before)
        if retry_count_min is not None:
            filters.append(Job.retry_count >= retry_count_min)
        if retry_count_max is not None:
            filters.append(Job.retry_count <= retry_count_max)

        where = and_(*filters)
        total = await self._count(where)

        stmt = (
            select(Job)
            .where(where)
            .order_by(Job.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total

    async def update_status(
        self,
        job_id: uuid.UUID,
        status: str,
        extra: dict[str, Any] | None = None,
    ) -> Job | None:
        values: dict[str, Any] = {"status": status}
        if extra:
            values.update(extra)
        if status == "running" and "started_at" not in values:
            values["started_at"] = datetime.utcnow()
        if status in ("completed", "failed", "dead_letter") and "completed_at" not in values:
            values["completed_at"] = datetime.utcnow()

        await self.session.execute(
            update(Job).where(Job.id == job_id).values(**values)
        )
        await self.session.flush()
        return await self.get_by_id(job_id)

    async def dlq_stats(self, tenant_id: uuid.UUID) -> tuple[int, dict[str, int]]:
        """Total DLQ count plus per-job-type breakdown, scoped to one tenant."""
        stmt = (
            select(Job.type, func.count().label("n"))
            .where(
                Job.status == JobStatus.DEAD_LETTER,
                Job.tenant_id == tenant_id,
            )
            .group_by(Job.type)
        )
        result = await self.session.execute(stmt)
        by_type = {row.type: int(row.n) for row in result.all()}
        return sum(by_type.values()), by_type
