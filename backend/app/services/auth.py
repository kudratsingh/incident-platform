"""Auth service — registration, login, token refresh and tenant enrolment.

Every path here also writes the audit row for what it did, and names the tenant
on its own transaction so the row is written under row-level security.
"""

import uuid

from app.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
)
from app.core.logging import get_logger, request_id_var, user_id_var
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.core.tenant_scope import declare_tenant_scope
from app.models.tenant import DEFAULT_TENANT_SLUG
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository

logger = get_logger(__name__)


class AuthService:
    """Who may hold an account here, and what a valid login is worth."""

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
        """Sign someone up, either founding a brand-new tenant or joining the
        shared default one. Any other tenant needs an invitation."""
        # Registration never takes a caller-supplied role (X-01 / F1-04).
        role = "user"
        existing = await self.user_repo.get_by_email(email)
        if existing:
            raise ConflictError(f"Email already registered: {email}")

        tenant = await self.tenant_repo.get_by_slug(tenant_slug)
        if tenant is None and new_tenant_name:
            # Self-service bootstrap: the registrant becomes the tenant's admin
            # so there is always an operator. Deliberately NOT is_platform_admin.
            tenant = await self.tenant_repo.create(
                slug=tenant_slug, name=new_tenant_name, is_active=True
            )
            role = "admin"
        elif tenant is not None and tenant_slug != DEFAULT_TENANT_SLUG:
            # WO-R2-25 / ADR 0024: this endpoint is unauthenticated and
            # `tenant_slug` is free-form, so naming an existing slug used to
            # enrol the caller into someone else's tenant. Public self-enrolment
            # is now the founder branch or the default tenant only; anything else
            # goes through `add_tenant_member`. 403 not 404 — the caller is being
            # refused, and ADR 0024 accepts that small disclosure.
            raise AuthorizationError(
                f"Registration into tenant {tenant_slug!r} requires an "
                "invitation from one of its administrators"
            )
        if tenant is None or not tenant.is_active:
            raise NotFoundError(f"Tenant {tenant_slug} not found or inactive")

        user = await self.user_repo.create(
            email=email,
            hashed_password=hash_password(password),
            role=role,
            tenant_id=tenant.id,
        )
        # RLS context for the audit write below — registration is unauthenticated,
        # so nothing set `app.tenant_id` and the INSERT was unisolated (WO-R2-129).
        await declare_tenant_scope(self.audit_repo.session, tenant.id)
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

    async def add_tenant_member(
        self,
        admin: User,
        email: str,
        password: str,
        ip_address: str | None = None,
    ) -> User:
        """Enrol a user into the admin's own tenant (WO-R2-25, ADR 0024).

        The authenticated counterpart to `register`. `tenant_id` is read off the
        authenticated admin — there is deliberately no tenant field to supply — and `role`
        is hard-coded to `user` (X-01 / F1-04). The audit row names the admin as `user_id`
        and the new account as `resource_id`. Chosen initial password, not a real invite.
        """
        existing = await self.user_repo.get_by_email(email)
        if existing:
            raise ConflictError(f"Email already registered: {email}")

        user = await self.user_repo.create(
            email=email,
            hashed_password=hash_password(password),
            role="user",
            tenant_id=admin.tenant_id,
        )
        await self.audit_repo.log(
            "user.enrolled",
            user_id=admin.id,
            tenant_id=admin.tenant_id,
            resource_type="user",
            resource_id=str(user.id),
            request_id=request_id_var.get("") or None,
            ip_address=ip_address,
            extra_data={"email": email, "role": "user"},
        )
        logger.info(
            "tenant member enrolled",
            extra={
                "email": email,
                "tenant_id": str(admin.tenant_id),
                "enrolled_by": str(admin.id),
            },
        )
        return user

    async def login(
        self, email: str, password: str, ip_address: str | None = None
    ) -> tuple[str, str]:
        """Check the password and return a fresh (access, refresh) token pair."""
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
        # Same reason as register(): login authenticates itself, so this
        # audit INSERT had no tenant context and no RLS backstop.
        await declare_tenant_scope(self.audit_repo.session, user.tenant_id)
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
        """Trade a valid refresh token for a new pair, if the account is still
        active."""
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
