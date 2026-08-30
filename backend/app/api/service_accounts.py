"""
Admin endpoints for machine principals — service accounts + their tokens.

Service-account management is a platform-operator workflow: every endpoint
here requires `is_platform_admin` (X-01 hop 2 — a tenant admin must not be
able to mint machine credentials). Platform admins can cross tenants via
`?tenant_id=`; the resolver enforces this the same way the other admin
endpoints do.

`chaos:invoke` is additionally refused as a grantable scope at this API
boundary while the chaos gate is closed (X-01 hop 3) — see
`assert_api_grantable`. The operator seed script provisions it through the
service layer and is unaffected.

Everything under this router is human-facing (creating and revoking machine
credentials is an operator workflow). The scope-guarded endpoints that
service accounts *use* live elsewhere (Wave 1 PR #3 onwards, `get_consumer_lag`
first).
"""

import uuid
from datetime import timedelta

from app.config import get_settings
from app.core.exceptions import AuthorizationError, NotFoundError
from app.core.scopes import assert_api_grantable
from app.dependencies import (
    get_db,
    require_platform_admin,
    resolve_admin_tenant,
)
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.schemas.common import MAX_PAGE_SIZE, PaginatedResponse
from app.schemas.service_account import (
    ServiceAccountCreate,
    ServiceAccountResponse,
    ServiceAccountScopesUpdate,
    TokenMintRequest,
    TokenMintResponse,
    TokenResponse,
)
from app.services.service_account import ServiceAccountService
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/admin/service-accounts", tags=["service-accounts"])

# Platform admin, not tenant admin: machine credentials are cross-cutting
# operator material, and `resolve_admin_tenant`'s `?tenant_id=` override
# already only honors platform admins.
_require_admin = require_platform_admin


def _assert_api_grantable(scopes: list[str] | None) -> None:
    """Endpoint-boundary gate on scope grants (create / PATCH / mint).

    Maps the ValueError from `assert_api_grantable` to 403 exactly as the
    service maps `validate_scopes`. Lives at the API layer ONLY — the
    seed script's service-layer provisioning path must stay open.
    """
    try:
        assert_api_grantable(
            scopes, chaos_enabled=get_settings().chaos_enabled
        )
    except ValueError as exc:
        raise AuthorizationError(str(exc)) from exc


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
    _assert_api_grantable(payload.scopes)
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
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
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


@router.patch(
    "/{sa_id}",
    response_model=ServiceAccountResponse,
)
async def update_service_account_scopes(
    sa_id: uuid.UUID,
    payload: ServiceAccountScopesUpdate,
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> ServiceAccountResponse:
    """Replace the service account's scope set.

    Existing tokens keep the scope subset they were minted with —
    tokens are immutable and independently scoped. Fresh tokens
    (POST /{id}/tokens) pick up the new set. Common use:

      1. `PATCH /admin/service-accounts/{id}` with the wider scope
         list (e.g. add `actions:execute` when the agent moves into
         Phase 6 remediation; `chaos:invoke` can only be added this
         way on a chaos-enabled stack — see `assert_api_grantable`)
      2. `POST /admin/service-accounts/{id}/tokens` to mint a fresh
         token that carries the new scopes
      3. Point the agent's PLATFORM_TOKEN at the new value

    Idempotent — a call that changes nothing skips the audit row.
    """
    _assert_api_grantable(payload.scopes)
    effective_tenant = await resolve_admin_tenant(current_user, db, tenant_id)
    sa = await ServiceAccountRepository(db).get_by_id(sa_id)
    if sa is None or sa.tenant_id != effective_tenant:
        raise NotFoundError(f"Service account not found: {sa_id}")
    updated = await _service(db).update_scopes(
        service_account=sa,
        scopes=payload.scopes,
        updated_by_user_id=current_user.id,
    )
    return ServiceAccountResponse.model_validate(updated)


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
    # Explicitly requested scopes are gated here; a `scopes: null` mint
    # inherits the account's set, which is fine — the account can only
    # hold chaos:invoke if the operator path (or a chaos-enabled admin)
    # legitimately put it there, and the service enforces the subset rule.
    _assert_api_grantable(payload.scopes)
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
