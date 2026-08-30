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
        elif tenant is not None and tenant_slug != DEFAULT_TENANT_SLUG:
            # WO-R2-25 / ADR 0024. This endpoint is unauthenticated, and
            # `tenant_slug` is a free-form string from the request body, so
            # before this branch existed anyone could name any tenant they
            # liked and be enrolled into it. No auth, no invite, no domain
            # check — the founder branch above only fires when the slug is
            # *free*, so naming a slug that already existed was precisely the
            # path that joined someone else's tenant.
            #
            # Public self-enrolment is therefore allowed into exactly two
            # places: a brand-new tenant (the founder branch above, where
            # there is nobody to harm) and the shared default tenant (which
            # is open by design — it is the demo/self-serve pool). Joining
            # any *other* existing tenant now requires an authenticated admin
            # of that tenant to do it, via `AuthService.add_tenant_member`.
            #
            # 403 rather than 404: the caller is being refused, not told the
            # tenant is missing. ADR 0024 records why that (small) disclosure
            # is accepted rather than papered over with a lie.
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
        # RLS context for the audit write below. Registration is
        # unauthenticated, so nothing has set `app.tenant_id` on this
        # transaction — before WO-R2-129 the INSERT was carried by the
        # policy's bootstrap branch, i.e. written with no isolation at
        # all. The tenant is known here; name it.
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

        The authenticated counterpart to `register`, and the reason closing
        public self-enrolment is not a functional regression: without it a
        founder could create a tenant and then never add a single colleague
        to it, because the only enrolment path in the system was the one this
        order shuts.

        Two properties do the security work, and both are about where the
        inputs come from rather than what they contain:

        * `tenant_id` is read off the **authenticated admin**, never off the
          request. There is deliberately no tenant field to supply, so this
          endpoint cannot be pointed at a tenant the caller does not
          administer — the defect being fixed was exactly a tenant identifier
          that the caller got to choose.
        * `role` is hard-coded to `user`, exactly as in `register`. An admin
          may grow their own tenant; they may not mint a second admin here,
          and no request body can ask for one (X-01 / F1-04).

        The audit row names both parties: `user_id` is the admin who acted,
        because that is the accountable identity, and `resource_id` is the
        account created. A row that recorded only the new user would say a
        stranger appeared and not who let them in.

        This is admin provisioning with a chosen initial password, not a real
        invite: no token, no email round-trip, no expiry. ADR 0024 records
        that as the deliberate interim, and why a half-built invite flow
        would have been worse than an honest small one.
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
