import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, select, update

from app.models.job import Job
from app.repositories.base import BaseRepository


class JobRepository(BaseRepository[Job]):
    model = Job

    async def get_by_idempotency_key(self, key: str) -> Job | None:
        result = await self.session.execute(
            select(Job).where(Job.idempotency_key == key)
        )
        return result.scalar_one_or_none()

    async def list_jobs(
        self,
        offset: int = 0,
        limit: int = 20,
        user_id: uuid.UUID | None = None,
        status: str | None = None,
        job_type: str | None = None,
        trace_id: str | None = None,
    ) -> tuple[list[Job], int]:
        filters = []
        if user_id is not None:
            filters.append(Job.user_id == user_id)
        if status is not None:
            filters.append(Job.status == status)
        if job_type is not None:
            filters.append(Job.type == job_type)
        if trace_id is not None:
            filters.append(Job.trace_id == trace_id)

        where = and_(*filters) if filters else None
        total = await self._count(where) if where is not None else await self._count()

        stmt = select(Job).order_by(Job.created_at.desc()).offset(offset).limit(limit)
        if where is not None:
            stmt = stmt.where(where)

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
