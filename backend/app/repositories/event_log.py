"""Repository for the immutable job_events log."""

import uuid

from app.models.event_log import JobEvent
from app.repositories.base import BaseRepository
from sqlalchemy import select


class EventLogRepository(BaseRepository[JobEvent]):
    model = JobEvent

    async def timeline(self, job_id: uuid.UUID, limit: int = 500) -> list[JobEvent]:
        """All events for a job in recorded order — the event-sourcing replay."""
        stmt = (
            select(JobEvent)
            .where(JobEvent.job_id == job_id)
            .order_by(JobEvent.recorded_at.asc(), JobEvent.kafka_offset.asc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
