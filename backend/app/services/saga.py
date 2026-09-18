"""
Saga service — composes a multi-step workflow as a chain of dependent jobs.

A Saga row plus N Job rows sharing its `saga_id`, each depending on the previous, so
DependencyResolver drives the steps and SagaCoordinator owns compensation: one
`{type}.compensate` job per completed step, in reverse order (processors are the app's).
"""

import uuid
from dataclasses import dataclass
from typing import Any

from app.core.exceptions import RequestValidationError
from app.core.logging import get_logger
from app.models.enums import SagaStatus
from app.models.saga import Saga
from app.repositories.audit import AuditRepository
from app.repositories.saga import SagaRepository
from app.services.job import JobService

logger = get_logger(__name__)


@dataclass(slots=True)
class SagaStep:
    """One step of a workflow as the caller declares it, before it becomes a
    job row."""

    type: str
    payload: dict[str, Any] | None = None
    priority: int = 0


class SagaService:
    """Builds sagas. Everything after creation is the coordinator's job."""

    def __init__(
        self,
        saga_repo: SagaRepository,
        job_service: JobService,
        audit_repo: AuditRepository,
    ) -> None:
        self.saga_repo = saga_repo
        self.job_service = job_service
        self.audit_repo = audit_repo

    async def create_saga(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        name: str,
        steps: list[SagaStep],
    ) -> Saga:
        """Write the saga and one job per step, each depending on the one
        before it, so the dependency resolver runs them in order."""
        if not steps:
            raise RequestValidationError("Saga must have at least one step")

        saga = await self.saga_repo.create(
            tenant_id=tenant_id, name=name, status=SagaStatus.RUNNING
        )

        prev_id: uuid.UUID | None = None
        # `saga_step_index` is declaration order written down: every step below is
        # inserted in one transaction, so `created_at` is a total tie (WO-R2-58).
        for index, step in enumerate(steps):
            job = await self.job_service.create_job(
                user_id=user_id,
                tenant_id=tenant_id,
                job_type=step.type,
                payload=step.payload,
                priority=step.priority,
                dependencies=[prev_id] if prev_id else None,
                saga_id=saga.id,
                saga_step_index=index,
            )
            prev_id = job.id

        await self.audit_repo.log(
            "saga.created",
            tenant_id=tenant_id,
            user_id=user_id,
            resource_type="saga",
            resource_id=str(saga.id),
            extra_data={"name": name, "step_count": len(steps)},
        )
        logger.info(
            "saga created",
            extra={
                "saga_id": str(saga.id),
                "saga_name": name,
                "step_count": len(steps),
            },
        )
        return saga
