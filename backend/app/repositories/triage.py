"""Repository for the LLM triage analyses."""

import uuid
from collections.abc import Sequence
from typing import Any

from app.models.triage import JobTriage
from app.repositories.base import BaseRepository
from sqlalchemy import select


class TriageRepository(BaseRepository[JobTriage]):
    model = JobTriage

    async def get_by_job_id(self, job_id: uuid.UUID) -> JobTriage | None:
        stmt = select(JobTriage).where(JobTriage.job_id == job_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def map_by_job_ids(
        self, job_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, JobTriage]:
        """Triage rows for a page of jobs, keyed by job id.

        One statement for the page rather than one per row: the admin job list widened
        to carry triage (WO-R3-312), and a per-row `get_by_job_id` would be a query per
        DLQ entry on a list the console polls. Missing ids are simply absent.
        """
        if not job_ids:
            return {}
        result = await self.session.execute(
            select(JobTriage).where(JobTriage.job_id.in_(list(job_ids)))
        )
        return {row.job_id: row for row in result.scalars().all()}

    async def upsert(
        self,
        *,
        job_id: uuid.UUID,
        tenant_id: uuid.UUID,
        root_cause_category: str,
        summary: str,
        suggested_fix: str,
        is_retryable: bool,
        confidence: float,
        model_used: str,
        usage: dict[str, Any] | None,
    ) -> JobTriage:
        """Insert a new triage, or update the existing one for this job.

        The UNIQUE on job_id makes a race between two consumers land on one row.
        """
        existing = await self.get_by_job_id(job_id)
        if existing is not None:
            existing.root_cause_category = root_cause_category
            existing.summary = summary
            existing.suggested_fix = suggested_fix
            existing.is_retryable = is_retryable
            existing.confidence = confidence
            existing.model_used = model_used
            existing.usage = usage
            await self.session.flush()
            return existing

        triage = JobTriage(
            job_id=job_id,
            tenant_id=tenant_id,
            root_cause_category=root_cause_category,
            summary=summary,
            suggested_fix=suggested_fix,
            is_retryable=is_retryable,
            confidence=confidence,
            model_used=model_used,
            usage=usage,
        )
        self.session.add(triage)
        await self.session.flush()
        return triage
