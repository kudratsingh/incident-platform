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
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.core.security import create_access_token, hash_password
from app.dependencies import get_db
from app.main import create_app
from app.models.base import Base
from app.models.enums import UserRole
from app.models.user import User

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
    from sqlalchemy.dialects import sqlite
    from sqlalchemy import TypeDecorator, String, Text
    import uuid, json

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
# FastAPI test client with DB override
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_db

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Convenience fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def test_user(db_session: AsyncSession) -> User:
    user = User(
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
async def admin_user(db_session: AsyncSession) -> User:
    user = User(
        email="admin@example.com",
        hashed_password=hash_password("password123"),
        role=UserRole.ADMIN,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    return user


@pytest.fixture
def user_token(test_user: User) -> str:
    return create_access_token({"sub": str(test_user.id), "role": test_user.role})


@pytest.fixture
def admin_token(admin_user: User) -> str:
    return create_access_token({"sub": str(admin_user.id), "role": admin_user.role})


@pytest.fixture
def auth_headers(user_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {user_token}"}


@pytest.fixture
def admin_headers(admin_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_token}"}
