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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_settings = get_settings()
_engine = create_async_engine(
    _settings.database_url,
    echo=_settings.debug,
    pool_pre_ping=True,
)
SQLAlchemyInstrumentor().instrument(engine=_engine.sync_engine)
_async_session = async_sessionmaker(_engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with _async_session() as session:
        async with session.begin():
            yield session
        # The commit has landed. Anything a service deferred because it
        # must not be visible before then — cache invalidation, today —
        # runs here (R2-23). On rollback the block above raises and this
        # never runs, which is the behaviour we want: nothing committed,
        # nothing to announce.
        #
        # This covers the API app and the MCP server, which shares this
        # dependency. The worker loops own their own `session.begin()`
        # blocks and call `run_post_commit` themselves.
        await run_post_commit(session)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


async def get_current_user(
    token: str = Depends(_oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
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

    # Guard against tenant-claim drift: if the token's tenant_id doesn't match
    # the user's actual tenant_id, refuse the request rather than silently
    # picking one. Tokens minted before the multi-tenancy migration won't
    # carry the claim — accept them only when they match the user's tenant.
    token_tenant_id = payload.get("tenant_id")
    if token_tenant_id is not None and token_tenant_id != str(user.tenant_id):
        raise AuthenticationError("Token tenant_id does not match user")

    user_id_var.set(str(user.id))
    tenant_id_var.set(str(user.tenant_id))

    # Postgres row-level security. The policies on tenant-scoped tables read
    # `current_setting('app.tenant_id', true)` and gate every row by it.
    # Setting it here means any query that escapes the repository helpers
    # (raw SQL, a forgotten filter) still can't leak across tenants.
    # Silent no-op on SQLite (tests) — `SET LOCAL` doesn't exist there.
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        await db.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(user.tenant_id)},
        )
    return user


# Re-export so callers import from one place
get_redis = _get_redis


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the shared session factory — used by the worker (no HTTP context)."""
    return _async_session


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
    """Dependency for endpoints that may cross tenant boundaries.

    Platform admins additionally bypass the per-tenant RLS scope when they
    pass `?tenant_id=` on the few endpoints that accept it. Ordinary
    `role=admin` users remain scoped to their own tenant.
    """
    if not current_user.is_platform_admin:
        raise AuthorizationError("Platform admin role required")
    return current_user


# ---------------------------------------------------------------------------
# Machine-principal auth
#
# Machine principals (service accounts) speak to the platform with opaque
# `sa_<random>` bearer tokens. The auth dependency below routes on the token
# prefix: JWT → user path (existing), sa_ → service-account path (new).
# Downstream code that doesn't care which kind of principal is calling
# depends on `get_current_principal`; endpoints reserved for humans keep
# depending on `get_current_user`.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """Unified caller identity — either a human `User` or a `ServiceAccount`.

    Every request is one or the other. The scope check (`require_scope`)
    reads `.scopes` and refuses human callers by construction; the tenant
    context vars and RLS setting are populated the same way for both."""

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
    """Unified auth entry point. Route on token prefix.

    Endpoints scope-guarded with `require_scope` chain off this dependency
    so both human and machine callers are recognized (the scope check then
    refuses humans). Endpoints strictly for humans keep depending on
    `get_current_user`, which continues to reject sa_ tokens by decoding
    them as JWTs and failing."""
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
    """Factory that returns a dependency requiring the caller's token carries
    every listed scope. Refuses human callers — scopes are machine-only
    (see ADR 0007)."""

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
    """Resolve the tenant_id an admin request should run against.

    Platform admins may override via `?tenant_id=...`; for ordinary admins
    and support users the override is silently ignored (we never want a
    misconfigured client to escalate scope by accident).

    When the effective tenant differs from the user's own, we also re-issue
    `set_config('app.tenant_id', ...)` so the Postgres RLS policies let the
    cross-tenant query through. No-op on SQLite.
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

    `resolve_admin_tenant` already computed this, but only where a handler
    remembered to call it, and three read paths did not (WO-R2-50):
    `GET /sagas/{id}` served any saga to any authenticated caller,
    `GET /sagas` passed `user_id=None` for privileged callers with no tenant
    filter at all, and `GET /admin/users/{id}/stats` answered any user UUID
    out of Redis. Declaring the scope as a dependency rather than as three
    remembered calls is the point: the next read endpoint inherits it by
    typing `Depends(get_effective_tenant)`, and forgetting it is visible in
    the signature rather than buried in a body.

    The value is the caller's own tenant, except that a platform admin may
    ask for another via `?tenant_id=` — the existing, deliberate override,
    which also retargets `app.tenant_id` so RLS admits the query. Everyone
    else's `?tenant_id=` is silently ignored, so exposing the parameter on
    these endpoints grants nobody anything they did not already have.

    RLS remains the backstop underneath this, not the substitute for it: it
    is inert on SQLite, inert for a superuser connection, and — as the stats
    path showed — absent entirely from a Redis read.
    """
    return await resolve_admin_tenant(current_user, db, tenant_id)
