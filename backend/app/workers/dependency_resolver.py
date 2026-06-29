"""
Dependency resolver — Kafka-driven scheduler for the job dependency DAG.

A job created with dependencies starts in WAITING. This consumer watches
`job.completed` events and, for each completing parent, examines its
children. A child is promoted to PENDING (and a job.submitted event
emitted via the outbox) once every one of its dependencies has reached
COMPLETED.

Idempotency: re-delivery of the same job.completed event may re-promote
the same child. The promotion is guarded by a status check (we only flip
WAITING → PENDING), so the worst case is a redundant job.submitted in the
outbox. Combined with the dispatcher's per-job idempotency, the worker
still processes the child only once.
"""

import uuid
from typing import Any

from app.config import get_settings
from app.core.logging import get_logger
from app.models.enums import JobStatus
from app.repositories.job import JobRepository
from app.repositories.job_dependency import JobDependencyRepository
from app.repositories.outbox import OutboxRepository
from app.workers.kafka_consumer import BaseKafkaConsumer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = get_logger(__name__)


class DependencyResolver(BaseKafkaConsumer):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        settings = get_settings()
        super().__init__(
            topics=[settings.kafka_topic_job_completed],
            group_id=settings.kafka_consumer_group_dependency,
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
        parent_id_str = value.get("job_id")
        if not parent_id_str:
            return
        try:
            parent_id = uuid.UUID(parent_id_str)
        except ValueError:
            return

        settings = get_settings()
        async with self.session_factory() as session:
            async with session.begin():
                dep_repo = JobDependencyRepository(session)
                job_repo = JobRepository(session)
                outbox_repo = OutboxRepository(session)

                child_ids = await dep_repo.children_of(parent_id)
                for child_id in child_ids:
                    child = await job_repo.get_by_id(child_id)
                    if child is None or child.status != JobStatus.WAITING:
                        # Either already promoted, cancelled, or running. Skip.
                        continue
                    unmet = await dep_repo.unmet_count(child_id)
                    if unmet > 0:
                        continue

                    await job_repo.update_status(
                        child_id, JobStatus.PENDING, extra={"error_message": None}
                    )
                    await outbox_repo.add(
                        tenant_id=child.tenant_id,
                        topic=settings.kafka_topic_job_submitted,
                        key=str(child.user_id),
                        payload={
                            "event": "job.submitted",
                            "tenant_id": str(child.tenant_id),
                            "job_id": str(child.id),
                            "user_id": str(child.user_id),
                            "job_type": child.type,
                            "payload": dict(child.payload or {}),
                            "priority": child.priority,
                            "trace_id": child.trace_id,
                        },
                    )
                    logger.info(
                        "dependency-resolver promoted child",
                        extra={
                            "child_id": str(child.id),
                            "parent_id": str(parent_id),
                            "type": child.type,
                        },
                    )
