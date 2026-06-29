"""Repository for the LLM triage analyses."""

import uuid
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

        Re-triaging the same job (e.g. after Replay → DLQ again) replaces the
        prior analysis rather than accumulating rows. job_id has a UNIQUE
        constraint, so a race between two consumers either succeeds once and
        no-ops once, or both end up at the latest write — both are acceptable.
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
