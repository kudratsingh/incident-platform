import uuid

from app.dependencies import get_db, require_role, resolve_admin_tenant
from app.models.enums import UserRole
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.schemas.audit import AuditListParams, AuditLogResponse
from app.schemas.common import PaginatedResponse
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/audit", tags=["audit"])

_require_support_or_admin = require_role(UserRole.SUPPORT, UserRole.ADMIN)


@router.get("/logs", response_model=PaginatedResponse[AuditLogResponse])
async def list_audit_logs(
    params: AuditListParams = Depends(),
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(_require_support_or_admin),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[AuditLogResponse]:
    # Tenant scope is applied in the application layer (F1-02): support and
    # tenant admins always read their own tenant; platform admins may target
    # another tenant with ?tenant_id= (resolve_admin_tenant ignores the
    # override for everyone else).
    effective_tenant = await resolve_admin_tenant(current_user, db, tenant_id)
    repo = AuditRepository(db)
    logs, total = await repo.list_logs(
        offset=params.offset,
        limit=params.page_size,
        user_id=params.user_id,
        job_id=params.job_id,
        action=params.action,
        principal_type=params.principal_type,
        tenant_id=effective_tenant,
    )
    return PaginatedResponse.build(
        items=[AuditLogResponse.model_validate(log) for log in logs],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )
