"""
Saga service — composes a multi-step workflow as a chain of dependent jobs.

A saga is just a Saga row plus N Job rows that share its `saga_id`. Each
step depends on the previous via the job dependency DAG, so the existing
DependencyResolver drives the per-step transitions; SagaCoordinator
(see app/workers/saga_coordinator.py) handles overall saga state and
compensation.

Compensation policy:
  - When any step in a saga fails terminally (dead-letter), SagaCoordinator
    cancels remaining waiting steps and publishes one `{type}.compensate`
    job per already-completed step, in reverse order. The application
    must register compensation processors for those job types.
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
    type: str
    payload: dict[str, Any] | None = None
    priority: int = 0


class SagaService:
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
        name: str,
        steps: list[SagaStep],
    ) -> Saga:
        if not steps:
            raise RequestValidationError("Saga must have at least one step")

        saga = await self.saga_repo.create(name=name, status=SagaStatus.RUNNING)

        prev_id: uuid.UUID | None = None
        for step in steps:
            job = await self.job_service.create_job(
                user_id=user_id,
                job_type=step.type,
                payload=step.payload,
                priority=step.priority,
                dependencies=[prev_id] if prev_id else None,
                saga_id=saga.id,
            )
            prev_id = job.id

        await self.audit_repo.log(
            "saga.created",
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
