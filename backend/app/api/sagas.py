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
from app.schemas.job import JobResponse, validate_processor_payload
from app.services.job import JobService
from app.services.saga import SagaService, SagaStep
from app.utils.admission import JOB_CREATE_RATE_BUCKET, check_job_admission
from app.utils.rate_limit import rate_limiter
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/sagas", tags=["sagas"])

# Upper bound on steps in one saga. Every step is a `jobs` row, so without a
# bound a single request creates an unbounded number of them — and the request
# is admitted or refused as a batch, so the bound is also what keeps the quota
# pre-check from having to reason about an arbitrarily large N. Generous
# against real chains (the longest shipped saga is 3 steps) and small enough
# that one request can never be a bulk-insert vector.
MAX_SAGA_STEPS = 50


class SagaStepRequest(BaseModel):
    type: str
    payload: dict[str, Any] | None = None
    priority: int = Field(default=0, ge=0, le=100)

    @model_validator(mode="after")
    def _bound_payload(self) -> "SagaStepRequest":
        # A saga step goes SagaService -> JobService.create_job directly and
        # never constructs a JobCreate, so the bounds have to be applied here
        # too or POST /sagas is an open bypass of the POST /jobs limits.
        # `type` stays a plain str (compensation types like
        # "csv_upload.compensate" are not JobType members); unknown types are
        # a no-op in validate_processor_payload.
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
    page: int = 1,
    page_size: int = 20,
    current_user: User = Depends(get_current_user),
    effective_tenant: uuid.UUID = Depends(get_effective_tenant),
    db: AsyncSession = Depends(get_db),
) -> SagaListResponse:
    from app.models.enums import UserRole

    repo = SagaRepository(db)
    # Admins/support see all sagas IN THEIR TENANT, regular users see only
    # their own. "All sagas" used to mean all sagas anywhere: the handler
    # passed user_id=None and applied no tenant filter, leaving Postgres RLS
    # as the only barrier — and RLS is a backstop for this check, not a
    # replacement for it (WO-R2-50).
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

    Admission control is the same as `POST /jobs` and runs through the same
    guard, because this endpoint creates `jobs` rows just as that one does —
    it just creates several. Counting the saga as its steps is what makes the
    monthly cap enforceable: `_check_monthly_quota` counts every `Job` row, so
    saga steps were already consuming the cap that blocks `POST /jobs` while
    this endpoint was never blocked by it (WO-R2-12).

    The check runs before `create_saga` opens its transaction, so a saga is
    refused whole rather than committing part of its chain and then meeting
    the cap mid-loop.
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

    Scoped twice, because this response carries every step job's `payload`,
    `result` and `error_message` and used to be readable by any authenticated
    caller who knew a saga id (WO-R2-50):

      * to the caller's effective tenant — a privileged caller is privileged
        inside their own tenant, and a platform admin's `?tenant_id=` is the
        only way across;
      * to the caller themselves, unless they are admin/support — the same
        ownership rule `GET /sagas` has always applied to the list.

    404 rather than 403 on both, matching the job endpoints: a caller who is
    not entitled to the row is not entitled to learn that it exists.
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
