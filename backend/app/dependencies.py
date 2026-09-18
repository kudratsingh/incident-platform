"""Per-request DB session and caller identity (human or machine). Both auth
paths set `app.tenant_id` for Postgres row-level security.
"""

import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from app.config import get_settings
from app.core.exceptions import AuthenticationError, AuthorizationError
from app.core.logging import tenant_id_var, user_id_var
from app.core.redis import get_redis as _get_redis
from app.core.scopes import Scope
from app.core.security import decode_token
from app.models.enums import UserRole
from app.models.service_account import ServiceAccount
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.repositories.user import UserRepository
from app.services.service_account import (
    ServiceAccountService,
    looks_like_service_account_token,
)
from app.utils.post_commit import run_post_commit
from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_settings = get_settings()
_engine = create_async_engine(
    _settings.database_url,
    echo=_settings.debug,
    pool_pre_ping=True,
)
SQLAlchemyInstrumentor().instrument(engine=_engine.sync_engine)
_async_session = async_sessionmaker(_engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """One database session per request, inside one transaction. Work a service
    deferred until the commit lands runs here, and only on success."""
    async with _async_session() as session:
        async with session.begin():
            yield session
        # The commit has landed, so work a service deferred until then — cache
        # invalidation, today — runs here (R2-23); on rollback it never runs.
        # The worker loops own their `begin()` and call `run_post_commit`.
        await run_post_commit(session)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


async def get_current_user(
    token: str = Depends(_oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """The human behind this request, from the access token, with their tenant
    put on the session so row-level security binds every later query."""
    payload = decode_token(token, expected_type="access")

    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("Malformed token payload") from exc

    user = await UserRepository(db).get_by_id(user_id)
    if not user:
        raise AuthenticationError("User not found")
    if not user.is_active:
        raise AuthenticationError("Account is disabled")

    # Refuse on tenant-claim drift rather than silently picking one. Tokens
    # minted before multi-tenancy carry no claim; accept those.
    token_tenant_id = payload.get("tenant_id")
    if token_tenant_id is not None and token_tenant_id != str(user.tenant_id):
        raise AuthenticationError("Token tenant_id does not match user")

    user_id_var.set(str(user.id))
    tenant_id_var.set(str(user.tenant_id))

    # Postgres RLS: policies gate rows on
    # `current_setting('app.tenant_id', true)`, so a forgotten filter cannot
    # leak across tenants. No-op on SQLite.
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        await db.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(user.tenant_id)},
        )
    return user


# Re-export so callers import from one place
get_redis = _get_redis


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the shared, tenant-scoped session factory.

    Since WO-R2-129 a statement that has not set `app.tenant_id` is refused
    by the `tenant_isolation` policies; code that legitimately spans tenants
    asks for `app.core.tenant_scope.platform_session_factory` (ADR 0026).
    """
    return _async_session


def get_engine() -> AsyncEngine:
    """The one shared engine; `platform_session_factory` reuses this pool
    instead of opening a second (ADR 0015, 0026)."""
    return _engine


def require_role(*roles: UserRole) -> "type[User]":
    """Factory that returns a FastAPI dependency enforcing one of the given roles."""

    async def _dependency(
        current_user: User = Depends(get_current_user),
    ) -> User:
        if current_user.role not in roles:
            raise AuthorizationError(
                f"Required role: {[r.value for r in roles]}, got: {current_user.role}"
            )
        return current_user

    return _dependency  # type: ignore[return-value]


async def require_platform_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """Endpoints that may cross tenant boundaries: only a platform admin's
    `?tenant_id=` is honoured — `role=admin` stays in its own tenant."""
    if not current_user.is_platform_admin:
        raise AuthorizationError("Platform admin role required")
    return current_user


# Machine-principal auth: service accounts carry opaque `sa_<random>` bearer
# tokens, so `get_current_principal` routes on the token prefix. Human-only
# endpoints keep depending on `get_current_user`.


@dataclass(frozen=True)
class Principal:
    """Unified caller identity — a human `User` or a `ServiceAccount`.

    `require_scope` reads `.scopes`; RLS and context vars are set for both."""

    kind: str  # "user" | "service_account"
    tenant_id: uuid.UUID
    user: User | None = None
    service_account: ServiceAccount | None = None
    scopes: frozenset[str] = frozenset()

    @property
    def id(self) -> uuid.UUID:
        if self.kind == "user":
            assert self.user is not None
            return self.user.id
        assert self.service_account is not None
        return self.service_account.id


async def _apply_tenant_context(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Set contextvars + Postgres RLS setting for this request. Shared by
    both auth paths so machine and human principals get identical isolation."""
    tenant_id_var.set(str(tenant_id))
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        await db.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )


async def get_current_principal(
    token: str = Depends(_oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> Principal:
    """Unified auth entry point, routing on the token prefix.

    `require_scope` endpoints chain off this so machine and human callers are
    both recognised; human-only endpoints keep `get_current_user`."""
    if looks_like_service_account_token(token):
        service = ServiceAccountService(
            ServiceAccountRepository(db),
            ServiceAccountTokenRepository(db),
            AuditRepository(db),
        )
        sa, sa_token = await service.verify_token(token)
        user_id_var.set(str(sa.id))
        await _apply_tenant_context(db, sa.tenant_id)
        return Principal(
            kind="service_account",
            tenant_id=sa.tenant_id,
            service_account=sa,
            scopes=frozenset(sa_token.scopes),
        )

    user = await get_current_user(token=token, db=db)
    return Principal(
        kind="user",
        tenant_id=user.tenant_id,
        user=user,
    )


def require_scope(*required: Scope) -> "type[Principal]":
    """Requires every listed scope; refuses human callers (ADR 0007)."""

    required_strs = frozenset(s.value for s in required)

    async def _dependency(
        principal: Principal = Depends(get_current_principal),
    ) -> Principal:
        if principal.kind != "service_account":
            raise AuthorizationError(
                "This endpoint requires a service-account token"
            )
        missing = required_strs - principal.scopes
        if missing:
            raise AuthorizationError(
                f"Missing required scope(s): {sorted(missing)}"
            )
        return principal

    return _dependency  # type: ignore[return-value]


async def resolve_admin_tenant(
    current_user: User,
    db: AsyncSession,
    requested: uuid.UUID | None,
) -> uuid.UUID:
    """The tenant_id an admin request runs against.

    Only a platform admin's `?tenant_id=` is honoured; a cross-tenant value
    also re-issues `set_config('app.tenant_id', ...)` so RLS admits the query.
    """
    if requested is None or not current_user.is_platform_admin:
        return current_user.tenant_id
    if requested != current_user.tenant_id and db.bind is not None:
        if db.bind.dialect.name == "postgresql":
            await db.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(requested)},
            )
    return requested


async def get_effective_tenant(
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> uuid.UUID:
    """The tenant a read handler must scope itself to — as a dependency.

    A dependency rather than three remembered `resolve_admin_tenant` calls,
    because three read paths forgot it (WO-R2-50). The value is the caller's
    own tenant, except a platform admin's `?tenant_id=`; RLS stays a backstop.
    """
    return await resolve_admin_tenant(current_user, db, tenant_id)
