import uuid
from datetime import datetime

from app.models.digest import IncidentDigest
from app.models.job import Job
from app.repositories.base import BaseRepository
from sqlalchemy import and_, func, select


class DigestRepository(BaseRepository[IncidentDigest]):
    model = IncidentDigest

    async def list_for_tenant(
        self, tenant_id: uuid.UUID, limit: int = 20
    ) -> list[IncidentDigest]:
        result = await self.session.execute(
            select(IncidentDigest)
            .where(IncidentDigest.tenant_id == tenant_id)
            .order_by(IncidentDigest.window_end.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def latest_window_end(self, tenant_id: uuid.UUID) -> datetime | None:
        result = await self.session.execute(
            select(func.max(IncidentDigest.window_end)).where(
                IncidentDigest.tenant_id == tenant_id
            )
        )
        return result.scalar_one_or_none()

    async def window_stats(
        self,
        tenant_id: uuid.UUID,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[dict[str, int], dict[str, int], list[str]]:
        """Returns (by_status_counts, failed_by_type_counts, error_samples).

        The aggregates feed the digest LLM call. `error_samples` is the
        list of error_message strings for failed+dlq jobs in the window
        — the service fingerprints them itself before sending to the LLM.
        """
        in_window = and_(
            Job.tenant_id == tenant_id,
            Job.created_at >= window_start,
            Job.created_at <= window_end,
        )

        # status counts
        status_stmt = (
            select(Job.status, func.count().label("n"))
            .where(in_window)
            .group_by(Job.status)
        )
        status_rows = await self.session.execute(status_stmt)
        by_status = {row.status: int(row.n) for row in status_rows.all()}

        # failed-by-type counts
        type_stmt = (
            select(Job.type, func.count().label("n"))
            .where(in_window, Job.status.in_(("failed", "dead_letter")))
            .group_by(Job.type)
        )
        type_rows = await self.session.execute(type_stmt)
        failed_by_type = {row.type: int(row.n) for row in type_rows.all()}

        # error_message samples (deduplication happens in the service)
        err_stmt = (
            select(Job.error_message)
            .where(in_window, Job.error_message.is_not(None))
            .limit(1000)
        )
        err_rows = await self.session.execute(err_stmt)
        errors = [row[0] for row in err_rows.all() if row[0]]

        return by_status, failed_by_type, errors
