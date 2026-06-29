import uuid
from collections.abc import AsyncGenerator

from app.config import get_settings
from app.core.exceptions import AuthenticationError, AuthorizationError
from app.core.logging import tenant_id_var, user_id_var
from app.core.redis import get_redis as _get_redis
from app.core.security import decode_token
from app.models.enums import UserRole
from app.models.user import User
from app.repositories.user import UserRepository
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
