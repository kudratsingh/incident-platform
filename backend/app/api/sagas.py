"""Sagas: multi-step workflows composed of dependent jobs."""

import uuid
from typing import Any

from app.dependencies import get_current_user, get_db, get_redis
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.job import JobRepository
from app.repositories.job_dependency import JobDependencyRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.saga import SagaRepository
from app.schemas.job import JobResponse
from app.services.job import JobService
from app.services.saga import SagaService, SagaStep
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/sagas", tags=["sagas"])


class SagaStepRequest(BaseModel):
    type: str
    payload: dict[str, Any] | None = None
    priority: int = Field(default=0, ge=0, le=100)


class SagaCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    steps: list[SagaStepRequest] = Field(min_length=1)


class SagaResponse(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    created_at: str
    completed_at: str | None
    steps: list[JobResponse]


def _saga_service(db: AsyncSession, redis: Redis) -> SagaService:
    job_service = JobService(
        JobRepository(db),
        AuditRepository(db),
        OutboxRepository(db),
        redis,
        dep_repo=JobDependencyRepository(db),
    )
    return SagaService(SagaRepository(db), job_service, AuditRepository(db))


@router.post("", response_model=SagaResponse, status_code=201)
async def create_saga(
    body: SagaCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> SagaResponse:
    svc = _saga_service(db, redis)
    saga = await svc.create_saga(
        user_id=current_user.id,
        name=body.name,
        steps=[
            SagaStep(type=s.type, payload=s.payload, priority=s.priority)
            for s in body.steps
        ],
    )
    jobs = await SagaRepository(db).jobs(saga.id)
    return SagaResponse(
        id=saga.id,
        name=saga.name,
        status=saga.status,
        created_at=saga.created_at.isoformat(),
        completed_at=saga.completed_at.isoformat() if saga.completed_at else None,
        steps=[JobResponse.model_validate(j) for j in jobs],
    )


@router.get("/{saga_id}", response_model=SagaResponse)
async def get_saga(
    saga_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SagaResponse:
    from app.core.exceptions import NotFoundError

    saga = await SagaRepository(db).get_by_id(saga_id)
    if saga is None:
        raise NotFoundError(f"Saga {saga_id} not found")
    jobs = await SagaRepository(db).jobs(saga_id)
    return SagaResponse(
        id=saga.id,
        name=saga.name,
        status=saga.status,
        created_at=saga.created_at.isoformat(),
        completed_at=saga.completed_at.isoformat() if saga.completed_at else None,
        steps=[JobResponse.model_validate(j) for j in jobs],
    )
