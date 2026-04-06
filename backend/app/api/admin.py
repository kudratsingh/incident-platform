import uuid

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_redis, require_role
from app.models.enums import UserRole
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.job import JobRepository
from app.repositories.user import UserRepository
from app.schemas.common import PaginatedResponse
from app.schemas.job import AdminJobListParams, JobResponse
from app.schemas.user import UserResponse
from app.services.job import JobService

router = APIRouter(prefix="/admin", tags=["admin"])

_require_support_or_admin = require_role(UserRole.SUPPORT, UserRole.ADMIN)
_require_admin = require_role(UserRole.ADMIN)


def _job_service(db: AsyncSession, redis: Redis) -> JobService:
    return JobService(JobRepository(db), AuditRepository(db), redis)


@router.get("/jobs", response_model=PaginatedResponse[JobResponse])
async def admin_list_jobs(
    params: AdminJobListParams = Depends(),
    current_user: User = Depends(_require_support_or_admin),
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
        filter_user_id=params.user_id,
    )
    return PaginatedResponse.build(
        items=[JobResponse.model_validate(j) for j in jobs],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def admin_get_job(
    job_id: uuid.UUID,
    current_user: User = Depends(_require_support_or_admin),
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


@router.post("/jobs/{job_id}/replay", response_model=JobResponse)
async def replay_job(
    job_id: uuid.UUID,
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> JobResponse:
    svc = _job_service(db, redis)
    job = await svc.replay_job(job_id=job_id, requesting_user_id=current_user.id)
    return JobResponse.model_validate(job)


@router.post("/incidents/{job_id}/resolve", response_model=JobResponse)
async def resolve_incident(
    job_id: uuid.UUID,
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> JobResponse:
    svc = _job_service(db, redis)
    job = await svc.resolve_incident(job_id=job_id, requesting_user_id=current_user.id)
    return JobResponse.model_validate(job)


@router.get("/users", response_model=PaginatedResponse[UserResponse])
async def list_users(
    page: int = 1,
    page_size: int = 20,
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[UserResponse]:
    repo = UserRepository(db)
    users, total = await repo.list_all(offset=(page - 1) * page_size, limit=page_size)
    return PaginatedResponse.build(
        items=[UserResponse.model_validate(u) for u in users],
        total=total,
        page=page,
        page_size=page_size,
    )
