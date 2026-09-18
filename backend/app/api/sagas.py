"""Sagas: multi-step workflows composed of dependent jobs."""

import uuid
from typing import Any

from app.dependencies import (
    get_current_user,
    get_db,
    get_effective_tenant,
    get_redis,
)
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.job import JobRepository
from app.repositories.job_dependency import JobDependencyRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.saga import SagaRepository
from app.schemas.common import MAX_PAGE_SIZE
from app.schemas.job import JobResponse, validate_processor_payload
from app.services.job import JobService
from app.services.saga import SagaService, SagaStep
from app.utils.admission import JOB_CREATE_RATE_BUCKET, check_job_admission
from app.utils.rate_limit import rate_limiter
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, model_validator
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/sagas", tags=["sagas"])

# Upper bound on steps in one saga: every step is a `jobs` row, so without a
# bound one request is an unbounded bulk insert (and an unbounded quota
# pre-check).
MAX_SAGA_STEPS = 50


class SagaStepRequest(BaseModel):
    type: str
    payload: dict[str, Any] | None = None
    priority: int = Field(default=0, ge=0, le=100)

    @model_validator(mode="after")
    def _bound_payload(self) -> "SagaStepRequest":
        # A saga step never builds a JobCreate, so the bounds apply here too
        # or POST /sagas bypasses the POST /jobs limits. `type` stays str —
        # compensation types are not JobType members.
        validate_processor_payload(self.type, self.payload)
        return self


class SagaCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    steps: list[SagaStepRequest] = Field(min_length=1, max_length=MAX_SAGA_STEPS)


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


class SagaListItem(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    created_at: str
    completed_at: str | None
    step_count: int


class SagaListResponse(BaseModel):
    items: list[SagaListItem]
    total: int
    page: int
    page_size: int


@router.get("", response_model=SagaListResponse)
async def list_sagas(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    current_user: User = Depends(get_current_user),
    effective_tenant: uuid.UUID = Depends(get_effective_tenant),
    db: AsyncSession = Depends(get_db),
) -> SagaListResponse:
    from app.models.enums import UserRole

    repo = SagaRepository(db)
    # Admins/support see all sagas IN THEIR TENANT, others only their own.
    # This used to pass user_id=None with no tenant filter, leaving RLS as
    # the only barrier (WO-R2-50).
    privileged = current_user.role in (UserRole.ADMIN, UserRole.SUPPORT)
    sagas, total = await repo.list_for_user(
        user_id=None if privileged else current_user.id,
        tenant_id=effective_tenant,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    items: list[SagaListItem] = []
    for s in sagas:
        steps = await repo.jobs(s.id)
        items.append(
            SagaListItem(
                id=s.id,
                name=s.name,
                status=s.status,
                created_at=s.created_at.isoformat(),
                completed_at=s.completed_at.isoformat() if s.completed_at else None,
                step_count=len(steps),
            )
        )
    return SagaListResponse(items=items, total=total, page=page, page_size=page_size)


@router.post("", response_model=SagaResponse, status_code=201)
async def create_saga(
    body: SagaCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    _rl: None = Depends(
        rate_limiter(limit=30, window=60, key_prefix=JOB_CREATE_RATE_BUCKET)
    ),
) -> SagaResponse:
    """Create a saga and its chain of dependent jobs.

    Same admission guard as `POST /jobs`, counting the saga as its
    `len(steps)` jobs (WO-R2-12), and run before the transaction opens so a
    saga is refused whole rather than part-committed.
    """
    await check_job_admission(
        db, redis, current_user.tenant_id, job_count=len(body.steps)
    )
    svc = _saga_service(db, redis)
    saga = await svc.create_saga(
        user_id=current_user.id,
        tenant_id=current_user.tenant_id,
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
    effective_tenant: uuid.UUID = Depends(get_effective_tenant),
    db: AsyncSession = Depends(get_db),
) -> SagaResponse:
    """One saga and its steps.

    Scoped to the caller's effective tenant AND, unless they are
    admin/support, to themselves — this response carries every step's
    `payload`, `result` and `error_message` (WO-R2-50). 404 on both.
    """
    from app.core.exceptions import NotFoundError
    from app.models.enums import UserRole

    privileged = current_user.role in (UserRole.ADMIN, UserRole.SUPPORT)
    saga = await SagaRepository(db).get_for_tenant(
        saga_id,
        tenant_id=effective_tenant,
        user_id=None if privileged else current_user.id,
    )
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
