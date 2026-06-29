"""
Saga coordinator — drives saga-level state and compensation.

Subscribes to:
  - job.completed → if all steps in the saga are completed, mark saga COMPLETED.
  - job.dlq       → terminal failure of a step. Mark saga COMPENSATING,
                    cancel any still-WAITING/PENDING downstream steps, and
                    enqueue one `{type}.compensate` job per already-COMPLETED
                    prior step (reverse order — the most recent successful
                    step is the first to roll back).

Compensation jobs are ordinary jobs published via the outbox, so they
benefit from the same delivery guarantees as the originals. If no
processor is registered for `{type}.compensate`, the job will dead-letter,
which is the intended forcing function: applications must define their
compensation logic explicitly.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from app.config import get_settings
from app.core.logging import get_logger
from app.models.enums import JobStatus, SagaStatus
from app.repositories.audit import AuditRepository
from app.repositories.job import JobRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.saga import SagaRepository
from app.workers.kafka_consumer import BaseKafkaConsumer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = get_logger(__name__)


COMPENSATE_SUFFIX = ".compensate"


class SagaCoordinator(BaseKafkaConsumer):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        settings = get_settings()
        super().__init__(
            topics=[
                settings.kafka_topic_job_completed,
                settings.kafka_topic_job_dlq,
            ],
            group_id=settings.kafka_consumer_group_saga,
        )
        self.session_factory = session_factory

    async def handle_message(
        self,
        topic: str,
        key: str | None,
        value: dict[str, Any],
        **_kafka_meta: Any,
    ) -> None:
        if not isinstance(value, dict):
            return
        event = value.get("event")
        job_id_str = value.get("job_id")
        if not (event and job_id_str):
            return
        try:
            job_id = uuid.UUID(job_id_str)
        except ValueError:
            return

        async with self.session_factory() as session:
            async with session.begin():
                job_repo = JobRepository(session)
                job = await job_repo.get_by_id(job_id)
                if job is None or job.saga_id is None:
                    return  # not a saga step
                saga_repo = SagaRepository(session)
                saga = await saga_repo.get_by_id(job.saga_id)
                if saga is None or saga.status not in (SagaStatus.RUNNING,):
                    # Already settled (failed/completed/compensating) — ignore.
                    return

                if event == "job.completed":
                    await self._handle_completion(session, saga.id)
                elif event == "job.failed" and value.get("dead_lettered") is True:
                    await self._handle_failure(session, saga.id, job_id, job.user_id)

    async def _handle_completion(self, session: AsyncSession, saga_id: uuid.UUID) -> None:
        saga_repo = SagaRepository(session)
        all_jobs = await saga_repo.jobs(saga_id)
        if not all_jobs:
            return
        if all(j.status == JobStatus.COMPLETED for j in all_jobs):
            saga = await saga_repo.get_by_id(saga_id)
            assert saga is not None
            saga.status = SagaStatus.COMPLETED
            saga.completed_at = datetime.now(UTC)
            await session.flush()
            await AuditRepository(session).log(
                "saga.completed",
                tenant_id=saga.tenant_id,
                resource_type="saga",
                resource_id=str(saga_id),
                extra_data={"step_count": len(all_jobs)},
            )
            logger.info("saga completed", extra={"saga_id": str(saga_id)})

    async def _handle_failure(
        self,
        session: AsyncSession,
        saga_id: uuid.UUID,
        failed_job_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        settings = get_settings()
        saga_repo = SagaRepository(session)
        job_repo = JobRepository(session)
        outbox_repo = OutboxRepository(session)
        audit = AuditRepository(session)

        # Mark saga as compensating.
        saga = await saga_repo.get_by_id(saga_id)
        assert saga is not None
        saga.status = SagaStatus.COMPENSATING
        await session.flush()

        # Cancel any waiting/pending downstream steps so they never run.
        waiting = await saga_repo.waiting_steps(saga_id)
        for w in waiting:
            if w.id == failed_job_id:
                continue
            await job_repo.update_status(
                w.id, JobStatus.CANCELLED, extra={"error_message": "saga rollback"}
            )

        # Enqueue compensation jobs for already-completed prior steps,
        # in reverse order (most recent success rolls back first).
        completed = await saga_repo.completed_steps(saga_id)
        for done in reversed(completed):
            await outbox_repo.add(
                tenant_id=saga.tenant_id,
                topic=settings.kafka_topic_job_submitted,
                key=str(user_id),
                payload={
                    "event": "job.submitted",
                    "tenant_id": str(saga.tenant_id),
                    "job_id": str(uuid.uuid4()),
                    "user_id": str(user_id),
                    "job_type": f"{done.type}{COMPENSATE_SUFFIX}",
                    "payload": {
                        "compensates_job_id": str(done.id),
                        "saga_id": str(saga_id),
                    },
                    "priority": done.priority,
                    "trace_id": done.trace_id,
                },
            )

        await audit.log(
            "saga.compensating",
            tenant_id=saga.tenant_id,
            resource_type="saga",
            resource_id=str(saga_id),
            extra_data={
                "failed_job_id": str(failed_job_id),
                "compensations_emitted": len(completed),
                "cancelled_downstream": len(waiting) - 1,
            },
        )
        logger.warning(
            "saga compensating",
            extra={
                "saga_id": str(saga_id),
                "failed_job_id": str(failed_job_id),
                "compensations": len(completed),
            },
        )
