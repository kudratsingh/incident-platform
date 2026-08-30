from app.dependencies import get_current_user, get_db, require_role
from app.models.enums import UserRole
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.schemas.auth import LoginRequest, RefreshRequest, TokenResponse
from app.schemas.user import TenantMemberCreate, UserCreate, UserResponse
from app.services.auth import AuthService
from app.utils.rate_limit import rate_limiter
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/auth", tags=["auth"])


def _auth_service(db: AsyncSession) -> AuthService:
    return AuthService(
        UserRepository(db), AuditRepository(db), TenantRepository(db)
    )


@router.post("/register", response_model=UserResponse, status_code=201)
async def register(
    body: UserCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _rl: None = Depends(rate_limiter(limit=10, window=60, key_prefix="register")),
) -> UserResponse:
    svc = _auth_service(db)
    user = await svc.register(
        email=body.email,
        password=body.password,
        tenant_slug=body.tenant_slug,
        new_tenant_name=body.new_tenant_name,
        ip_address=request.client.host if request.client else None,
    )
    return UserResponse.model_validate(user)


@router.post(
    "/tenant/members", response_model=UserResponse, status_code=201
)
async def add_tenant_member(
    body: TenantMemberCreate,
    request: Request,
    current_user: User = Depends(require_role(UserRole.ADMIN)),
    db: AsyncSession = Depends(get_db),
    _rl: None = Depends(rate_limiter(limit=10, window=60, key_prefix="enrol")),
) -> UserResponse:
    """Enrol a user into the caller's own tenant.

    The authenticated way into an existing tenant, and since WO-R2-25 the
    only way: public registration may create a brand-new tenant or join the
    shared default one, and nothing else (ADR 0024).

    The tenant is taken from `current_user`, so there is no tenant parameter
    to tamper with, and the new account is always `role=user`.
    """
    svc = _auth_service(db)
    user = await svc.add_tenant_member(
        admin=current_user,
        email=body.email,
        password=body.password,
        ip_address=request.client.host if request.client else None,
    )
    return UserResponse.model_validate(user)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _rl: None = Depends(rate_limiter(limit=10, window=60, key_prefix="login")),
) -> TokenResponse:
    svc = _auth_service(db)
    access_token, refresh_token = await svc.login(
        email=body.email,
        password=body.password,
        ip_address=request.client.host if request.client else None,
    )
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    svc = _auth_service(db)
    access_token, refresh_token = await svc.refresh(body.refresh_token)
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.get("/me", response_model=UserResponse)
async def me(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    # The user.tenant relationship is lazy=noload, so explicitly fetch the slug
    # — the header chip in the frontend shows it.
    from app.models.tenant import Tenant
    from sqlalchemy import select

    slug = (
        await db.execute(select(Tenant.slug).where(Tenant.id == current_user.tenant_id))
    ).scalar_one_or_none()
    resp = UserResponse.model_validate(current_user)
    resp.tenant_slug = slug
    return resp
