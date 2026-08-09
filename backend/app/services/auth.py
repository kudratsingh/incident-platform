import uuid

from app.core.exceptions import AuthenticationError, ConflictError, NotFoundError
from app.core.logging import get_logger, request_id_var, user_id_var
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.tenant import DEFAULT_TENANT_SLUG
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository

logger = get_logger(__name__)


class AuthService:
    def __init__(
        self,
        user_repo: UserRepository,
        audit_repo: AuditRepository,
        tenant_repo: TenantRepository,
    ) -> None:
        self.user_repo = user_repo
        self.audit_repo = audit_repo
        self.tenant_repo = tenant_repo

    async def register(
        self,
        email: str,
        password: str,
        tenant_slug: str = DEFAULT_TENANT_SLUG,
        new_tenant_name: str | None = None,
        ip_address: str | None = None,
    ) -> User:
        # Registration never takes a caller-supplied role (X-01 / F1-04).
        # Everyone starts as a plain user; the single, bounded exception is
        # the founder branch below, which is decided here — not by the
        # request — when a brand-new tenant is created.
        role = "user"
        existing = await self.user_repo.get_by_email(email)
        if existing:
            raise ConflictError(f"Email already registered: {email}")

        tenant = await self.tenant_repo.get_by_slug(tenant_slug)
        if tenant is None and new_tenant_name:
            # Self-service tenant creation: register-time bootstrap.
            # The registering user becomes the tenant's admin so there's
            # always at least one operator. This deliberately does NOT
            # set is_platform_admin — that's reserved for the cross-tenant
            # operator role and is only granted by an existing platform admin.
            tenant = await self.tenant_repo.create(
                slug=tenant_slug, name=new_tenant_name, is_active=True
            )
            role = "admin"
        if tenant is None or not tenant.is_active:
            raise NotFoundError(f"Tenant {tenant_slug} not found or inactive")

        user = await self.user_repo.create(
            email=email,
            hashed_password=hash_password(password),
            role=role,
            tenant_id=tenant.id,
        )
        await self.audit_repo.log(
            "user.registered",
            user_id=user.id,
            tenant_id=tenant.id,
            resource_type="user",
            resource_id=str(user.id),
            request_id=request_id_var.get("") or None,
            ip_address=ip_address,
        )
        logger.info(
            "user registered",
            extra={"email": email, "role": role, "tenant_id": str(tenant.id)},
        )
        return user

    async def login(
        self, email: str, password: str, ip_address: str | None = None
    ) -> tuple[str, str]:
        user = await self.user_repo.get_by_email(email)
        if not user or not verify_password(password, user.hashed_password):
            raise AuthenticationError("Invalid email or password")
        if not user.is_active:
            raise AuthenticationError("Account is disabled")

        token_data = {
            "sub": str(user.id),
            "tenant_id": str(user.tenant_id),
            "role": user.role,
            "email": user.email,
        }
        access_token = create_access_token(token_data)
        refresh_token = create_refresh_token(token_data)

        user_id_var.set(str(user.id))
        await self.audit_repo.log(
            "user.login",
            user_id=user.id,
            tenant_id=user.tenant_id,
            resource_type="user",
            resource_id=str(user.id),
            request_id=request_id_var.get("") or None,
            ip_address=ip_address,
        )
        logger.info("user login", extra={"email": email, "tenant_id": str(user.tenant_id)})
        return access_token, refresh_token

    async def refresh(self, refresh_token: str) -> tuple[str, str]:
        payload = decode_token(refresh_token, expected_type="refresh")
        user_id = payload["sub"]
        user = await self.user_repo.get_by_id(uuid.UUID(user_id))
        if not user or not user.is_active:
            raise AuthenticationError("User not found or disabled")

        token_data = {
            "sub": str(user.id),
            "tenant_id": str(user.tenant_id),
            "role": user.role,
            "email": user.email,
        }
        return create_access_token(token_data), create_refresh_token(token_data)
