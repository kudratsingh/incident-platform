"""
Saga coordinator — saga-level state and compensation, off `job.completed` and `job.dlq`. A step's
terminal failure marks the saga COMPENSATING, cancels WAITING/PENDING downstream steps, and mints
one `{type}.compensate` job per already-COMPLETED step in the same transaction as its outbox row;
an unregistered `.compensate` processor dead-letters, the intended forcing function. Settlement is
drained-set (ADR 0017, WO-R2-49), and only compensation-typed events route at COMPENSATING.
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
    """Consumer group `saga-coordinator` — moves a saga through its own states
    and runs the rollback when a step dies."""

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
        """Route one step's event to the saga it belongs to: completion,
        failure, or the settling of a rollback already under way."""
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
                if saga is None:
                    return

                is_comp = job.type.endswith(COMPENSATE_SUFFIX)
                is_done = event == "job.completed"
                is_dlq = (
                    event == "job.failed" and value.get("dead_lettered") is True
                )

                if saga.status == SagaStatus.RUNNING and not is_comp:
                    if is_done:
                        await self._handle_completion(session, saga.id)
                    elif is_dlq:
                        await self._handle_failure(
                            session, saga.id, job_id, job.user_id
                        )
                elif (
                    saga.status == SagaStatus.COMPENSATING
                    and is_comp
                    and (is_done or is_dlq)
                ):
                    await self._settle_if_drained(session, saga.id)
                # Everything else is ignored — a redelivered original `job.dlq` would re-run
                # `_handle_failure`, so the type check IS the idempotency guard.

    async def _handle_completion(self, session: AsyncSession, saga_id: uuid.UUID) -> None:
        """Mark the saga COMPLETED once every one of its steps has."""
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
        """Start the rollback: cancel the steps still to come and queue one
        `.compensate` job per step that already succeeded, newest first."""
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

        # Cancel waiting/pending downstream steps. The audit count must come from the counter, never
        # `len(waiting)` — E1-13: `len(waiting) - 1` reported -1 with nothing downstream.
        cancelled = 0
        waiting = await saga_repo.waiting_steps(saga_id)
        for w in waiting:
            if w.id == failed_job_id:
                continue
            await job_repo.update_status(
                w.id, JobStatus.CANCELLED, extra={"error_message": "saga rollback"}
            )
            cancelled += 1

        # Compensate completed steps in reverse order. E1-02: the row must exist before the
        # dispatcher sees the event, or `_run_job` logs "job not found, skipping" and the rollback
        # never happens — so `create` flushes inside the ambient transaction.
        completed = await saga_repo.completed_steps(saga_id)
        for done in reversed(completed):
            comp_payload = {
                "compensates_job_id": str(done.id),
                "saga_id": str(saga_id),
            }
            comp_job = await job_repo.create(
                tenant_id=saga.tenant_id,
                user_id=user_id,
                type=f"{done.type}{COMPENSATE_SUFFIX}",
                status=JobStatus.PENDING,
                payload=comp_payload,
                priority=done.priority,
                max_attempts=done.max_attempts,
                trace_id=done.trace_id,
                saga_id=saga_id,
            )
            await outbox_repo.add(
                tenant_id=saga.tenant_id,
                topic=settings.kafka_topic_job_submitted,
                key=f"{saga.tenant_id}:{user_id}",
                payload={
                    "event": "job.submitted",
                    "tenant_id": str(saga.tenant_id),
                    "job_id": str(comp_job.id),
                    "user_id": str(user_id),
                    "job_type": f"{done.type}{COMPENSATE_SUFFIX}",
                    "payload": dict(comp_payload),
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
                "cancelled_downstream": cancelled,
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

        # WO-R2-49: with nothing completed the set is drained the moment it is built and no
        # `.compensate` event will arrive, so settle here or the saga pins at COMPENSATING forever.
        if not completed:
            await self._settle_if_drained(session, saga_id)

    async def _settle_if_drained(
        self, session: AsyncSession, saga_id: uuid.UUID
    ) -> None:
        """Settle a COMPENSATING saga once every `.compensate` job is terminal, INCLUDING the empty
        set. Recomputed each call, so a redelivery is harmless; only `.compensate` jobs count."""
        saga_repo = SagaRepository(session)
        all_jobs = await saga_repo.jobs(saga_id)
        comp_jobs = [j for j in all_jobs if j.type.endswith(COMPENSATE_SUFFIX)]
        terminal = (JobStatus.COMPLETED, JobStatus.DEAD_LETTER, JobStatus.CANCELLED)
        if any(j.status not in terminal for j in comp_jobs):
            return  # rollback still in flight

        unsuccessful = sum(1 for j in comp_jobs if j.status != JobStatus.COMPLETED)
        saga = await saga_repo.get_by_id(saga_id)
        assert saga is not None
        # A saga whose rollback itself dead-lettered has NOT been compensated —
        # it is left dirty and needs a human. See ADR 0017.
        saga.status = (
            SagaStatus.COMPENSATED if unsuccessful == 0 else SagaStatus.FAILED
        )
        saga.completed_at = datetime.now(UTC)
        await session.flush()
        await AuditRepository(session).log(
            "saga.compensated" if unsuccessful == 0 else "saga.compensation_failed",
            tenant_id=saga.tenant_id,
            resource_type="saga",
            resource_id=str(saga_id),
            extra_data={
                "compensation_steps": len(comp_jobs),
                "dead_lettered": unsuccessful,
            },
        )
        logger.info(
            "saga compensation settled",
            extra={"saga_id": str(saga_id), "status": saga.status},
        )
