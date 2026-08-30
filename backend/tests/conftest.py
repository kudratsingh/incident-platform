"""
Shared pytest fixtures.

Unit tests (backend/tests/unit/) use mock repositories — no DB required.
API tests (backend/tests/api/) spin up the full FastAPI app with dependency
overrides that swap in an in-memory SQLite session.  Because SQLite lacks
JSONB and UUID column types we configure SQLAlchemy to render those as JSON
and VARCHAR respectively; this is good enough for contract/shape testing.
Integration tests targeting real Postgres live in backend/tests/integration/.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from app.core.security import create_access_token, hash_password
from app.dependencies import get_db, get_redis, get_session_factory
from app.main import create_app
from app.models.base import Base
from app.models.enums import UserRole
from app.models.user import User
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

# ---------------------------------------------------------------------------
# SQLite in-memory engine (for API + shape tests)
# ---------------------------------------------------------------------------

_SQLITE_URL = "sqlite+aiosqlite://"


@pytest_asyncio.fixture(scope="session")
async def sqlite_engine():  # type: ignore[return]
    engine = create_async_engine(
        _SQLITE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # SQLite doesn't know UUID / JSONB — render them as strings/text

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(sqlite_engine) -> AsyncGenerator[AsyncSession, None]:  # type: ignore[return]
    session_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with session_factory() as session:
        async with session.begin():
            yield session
            await session.rollback()


# ---------------------------------------------------------------------------
# Postgres transaction semantics, on SQLite
# ---------------------------------------------------------------------------


class AbortingSession:
    """An `AsyncSession` that aborts its transaction the way Postgres does.

    Postgres refuses every statement after a failed one — `current
    transaction is aborted, commands ignored until end of transaction
    block` — until the transaction, or a SAVEPOINT enclosing the failure,
    is rolled back. SQLite has no such rule: a failed statement is just a
    failed statement, and the next one runs fine.

    That difference is why a whole class of bug (R2-59: a handler swallows
    a DB error, and every write the request makes afterwards is silently
    dropped) is invisible to this suite. This proxy forwards everything to
    a real session and adds exactly Postgres' rule, so a unit test can
    assert what production does.

    `fail_on` picks the statement that blows up — matching on rendered SQL,
    so it stands in for "the table isn't there" without needing a real
    migration state.
    """

    def __init__(
        self,
        inner: AsyncSession,
        *,
        fail_on: str,
        error: str = "relation does not exist",
    ) -> None:
        self._inner = inner
        self._fail_on = fail_on
        self._error = error
        self.aborted = False

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self._inner, name)

    def _guard(self) -> None:
        if self.aborted:
            raise ProgrammingError(
                "current transaction is aborted, commands ignored until "
                "end of transaction block",
                params=None,
                orig=Exception("InFailedSqlTransaction"),
            )

    async def execute(self, statement, *args, **kwargs):  # type: ignore[no-untyped-def]
        self._guard()
        if self._fail_on in str(statement):
            self.aborted = True
            raise ProgrammingError(
                self._error, params=None, orig=Exception(self._error)
            )
        try:
            return await self._inner.execute(statement, *args, **kwargs)
        except Exception:
            self.aborted = True
            raise

    async def flush(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self._guard()
        try:
            return await self._inner.flush(*args, **kwargs)
        except Exception:
            self.aborted = True
            raise

    def begin_nested(self):  # type: ignore[no-untyped-def]
        """A SAVEPOINT — and rolling back to one un-aborts the transaction,
        which is the entire point of the fix under test."""
        inner_cm = self._inner.begin_nested()
        outer = self

        @asynccontextmanager
        async def _wrapper() -> AsyncGenerator[None, None]:
            async with inner_cm:
                try:
                    yield
                except BaseException:
                    outer.aborted = False
                    raise

        return _wrapper()


class _SharedSessionFactory:
    """Stand-in for `get_session_factory` that reuses the test's session.

    A handler that opens its own session — the digest route does, so its
    Anthropic round-trip holds no transaction (WO-R2-127) — would otherwise
    reach the real engine and a database no test has set up.

    Each `factory()` hands back the one `db_session` every fixture and
    assertion already shares, with `begin()` demoted to a SAVEPOINT so a
    handler's "own transaction" nests inside the outer one the suite rolls
    back, and `close()` neutralised so the handler cannot close the session
    the test is still using. What the handler observes is unchanged: it opens
    a scope, writes, and the write is visible afterwards.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __call__(self) -> "_SharedSession":
        return _SharedSession(self._session)


class _SharedSession:
    def __init__(self, inner: AsyncSession) -> None:
        self._inner = inner

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self._inner, name)

    async def __aenter__(self) -> "_SharedSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def begin(self):  # type: ignore[no-untyped-def]
        return self._inner.begin_nested()

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# FastAPI test client with DB override
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client(  # type: ignore[no-untyped-def]
    db_session: AsyncSession, default_tenant
) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    async def _override_redis() -> AsyncGenerator[AsyncMock, None]:
        mock = AsyncMock()
        mock.zadd = AsyncMock(return_value=1)
        mock.zpopmax = AsyncMock(return_value=[])
        mock.zrangebyscore = AsyncMock(return_value=[])
        mock.zrem = AsyncMock(return_value=0)
        mock.incr = AsyncMock(return_value=1)
        mock.expire = AsyncMock(return_value=True)
        mock.get = AsyncMock(return_value=None)  # cache always misses in tests
        mock.set = AsyncMock(return_value=True)
        mock.delete = AsyncMock(return_value=1)
        yield mock

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis
    app.dependency_overrides[get_session_factory] = lambda: _SharedSessionFactory(
        db_session
    )

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Convenience fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def default_tenant(db_session: AsyncSession):  # type: ignore[no-untyped-def]
    """Ensure the default-tenant row exists before any user is inserted.

    Conftest uses Base.metadata.create_all rather than running migrations, so
    the seed INSERT from the f8a1c4e23507 migration doesn't run automatically.
    """
    from app.models.tenant import DEFAULT_TENANT_ID, Tenant
    from sqlalchemy import select

    existing = (
        await db_session.execute(select(Tenant).where(Tenant.id == DEFAULT_TENANT_ID))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    tenant = Tenant(
        id=DEFAULT_TENANT_ID,
        slug="default",
        name="Default Tenant",
        is_active=True,
    )
    db_session.add(tenant)
    await db_session.flush()
    return tenant


@pytest_asyncio.fixture
async def test_user(db_session: AsyncSession, default_tenant) -> User:  # type: ignore[no-untyped-def]
    user = User(
        tenant_id=default_tenant.id,
        email="user@example.com",
        hashed_password=hash_password("password123"),
        role=UserRole.USER,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession, default_tenant) -> User:  # type: ignore[no-untyped-def]
    # Matches the d9c01a7e4f30 migration: default-tenant admins become
    # platform admins on upgrade, so the test admin should too.
    user = User(
        tenant_id=default_tenant.id,
        email="admin@example.com",
        hashed_password=hash_password("password123"),
        role=UserRole.ADMIN,
        is_active=True,
        is_platform_admin=True,
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    return user


@pytest.fixture
def user_token(test_user: User) -> str:
    return create_access_token(
        {
            "sub": str(test_user.id),
            "tenant_id": str(test_user.tenant_id),
            "role": test_user.role,
        }
    )


@pytest.fixture
def admin_token(admin_user: User) -> str:
    return create_access_token(
        {
            "sub": str(admin_user.id),
            "tenant_id": str(admin_user.tenant_id),
            "role": admin_user.role,
        }
    )


@pytest.fixture
def auth_headers(user_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {user_token}"}


@pytest.fixture
def admin_headers(admin_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_token}"}
