"""
Admin endpoints for machine principals — service accounts + their tokens.

Tenant admins manage their own tenant's service accounts. Platform admins
can cross tenants via `?tenant_id=`; the resolver enforces this the same
way the other admin endpoints do.

Everything under this router is human-facing (creating and revoking machine
credentials is an operator workflow). The scope-guarded endpoints that
service accounts *use* live elsewhere (Wave 1 PR #3 onwards, `get_consumer_lag`
first).
"""

import uuid
from datetime import timedelta

from app.core.exceptions import NotFoundError
from app.dependencies import (
    get_db,
    require_role,
    resolve_admin_tenant,
)
from app.models.enums import UserRole
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.schemas.common import PaginatedResponse
from app.schemas.service_account import (
    ServiceAccountCreate,
    ServiceAccountResponse,
    TokenMintRequest,
    TokenMintResponse,
    TokenResponse,
)
from app.services.service_account import ServiceAccountService
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/admin/service-accounts", tags=["service-accounts"])

_require_admin = require_role(UserRole.ADMIN)


def _service(db: AsyncSession) -> ServiceAccountService:
    return ServiceAccountService(
        ServiceAccountRepository(db),
        ServiceAccountTokenRepository(db),
        AuditRepository(db),
    )


@router.post(
    "",
    response_model=ServiceAccountResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_service_account(
    payload: ServiceAccountCreate,
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> ServiceAccountResponse:
    effective_tenant = await resolve_admin_tenant(current_user, db, tenant_id)
    sa = await _service(db).create_service_account(
        tenant_id=effective_tenant,
        name=payload.name,
        scopes=payload.scopes,
        created_by_user_id=current_user.id,
    )
    return ServiceAccountResponse.model_validate(sa)


@router.get("", response_model=PaginatedResponse[ServiceAccountResponse])
async def list_service_accounts(
    tenant_id: uuid.UUID | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[ServiceAccountResponse]:
    effective_tenant = await resolve_admin_tenant(current_user, db, tenant_id)
    items, total = await ServiceAccountRepository(db).list_for_tenant(
        effective_tenant,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return PaginatedResponse.build(
        items=[ServiceAccountResponse.model_validate(sa) for sa in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/{sa_id}/tokens",
    response_model=TokenMintResponse,
    status_code=status.HTTP_201_CREATED,
)
async def mint_token(
    sa_id: uuid.UUID,
    payload: TokenMintRequest,
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> TokenMintResponse:
    effective_tenant = await resolve_admin_tenant(current_user, db, tenant_id)
    sa = await ServiceAccountRepository(db).get_by_id(sa_id)
    if sa is None or sa.tenant_id != effective_tenant:
        raise NotFoundError(f"Service account not found: {sa_id}")
    ttl = timedelta(days=payload.ttl_days) if payload.ttl_days else None
    token, plaintext = await _service(db).mint_token(
        service_account=sa,
        scopes=payload.scopes,
        ttl=ttl,
        minted_by_user_id=current_user.id,
    )
    return TokenMintResponse(
        token=TokenResponse.model_validate(token),
        plaintext=plaintext,
    )


@router.get(
    "/{sa_id}/tokens",
    response_model=list[TokenResponse],
)
async def list_tokens(
    sa_id: uuid.UUID,
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[TokenResponse]:
    effective_tenant = await resolve_admin_tenant(current_user, db, tenant_id)
    sa = await ServiceAccountRepository(db).get_by_id(sa_id)
    if sa is None or sa.tenant_id != effective_tenant:
        raise NotFoundError(f"Service account not found: {sa_id}")
    tokens = await ServiceAccountTokenRepository(db).list_for_service_account(sa.id)
    return [TokenResponse.model_validate(t) for t in tokens]


@router.delete(
    "/{sa_id}/tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_token(
    sa_id: uuid.UUID,
    token_id: uuid.UUID,
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    effective_tenant = await resolve_admin_tenant(current_user, db, tenant_id)
    sa = await ServiceAccountRepository(db).get_by_id(sa_id)
    if sa is None or sa.tenant_id != effective_tenant:
        raise NotFoundError(f"Service account not found: {sa_id}")
    await _service(db).revoke_token(
        service_account=sa,
        token_id=token_id,
        revoked_by_user_id=current_user.id,
    )
