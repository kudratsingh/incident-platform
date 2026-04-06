import uuid

from app.dependencies import get_current_user, get_db, get_redis
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.job import JobRepository
from app.schemas.common import PaginatedResponse
from app.schemas.job import JobCreate, JobListParams, JobResponse
from app.services.job import JobService
from app.utils.rate_limit import rate_limiter
from fastapi import APIRouter, Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _job_service(db: AsyncSession, redis: Redis) -> JobService:
    return JobService(JobRepository(db), AuditRepository(db), redis)


@router.post("", response_model=JobResponse, status_code=201)
async def create_job(
    body: JobCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    _rl: None = Depends(rate_limiter(limit=30, window=60, key_prefix="jobs:create")),
) -> JobResponse:
    svc = _job_service(db, redis)
    job = await svc.create_job(
        user_id=current_user.id,
        job_type=body.type,
        payload=body.payload,
        idempotency_key=body.idempotency_key,
        priority=body.priority,
    )
    return JobResponse.model_validate(job)


@router.get("", response_model=PaginatedResponse[JobResponse])
async def list_jobs(
    params: JobListParams = Depends(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> PaginatedResponse[JobResponse]:
    svc = _job_service(db, redis)
    jobs, total = await svc.list_jobs(
        requesting_user_id=current_user.id,
        user_role=current_user.role,
        page=params.page,
        page_size=params.page_size,
        status=params.status,
        job_type=params.type,
        trace_id=params.trace_id,
    )
    return PaginatedResponse.build(
        items=[JobResponse.model_validate(j) for j in jobs],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> JobResponse:
    svc = _job_service(db, redis)
    job = await svc.get_job(
        job_id=job_id,
        requesting_user_id=current_user.id,
        user_role=current_user.role,
    )
    return JobResponse.model_validate(job)
