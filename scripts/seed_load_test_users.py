"""
Create the two load-test accounts expected by locustfile.py.

Usage (with the stack running):
    python scripts/seed_load_test_users.py

Env vars (all optional — match the defaults in locustfile.py):
    DATABASE_URL          postgres+asyncpg://...  (defaults to docker-compose value)
    LOAD_USER_EMAIL       default: loadtest@example.com
    LOAD_USER_PASSWORD    default: LoadTest123!
    LOAD_ADMIN_EMAIL      default: loadtest-admin@example.com
    LOAD_ADMIN_PASSWORD   default: LoadTest123!
"""

from __future__ import annotations

import asyncio
import os
import sys

# Allow running from project root without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.core.security import hash_password  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.models.user import User  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker  # noqa: E402

_DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/incident_platform",
)

_USERS = [
    {
        "email": os.getenv("LOAD_USER_EMAIL", "loadtest@example.com"),
        "password": os.getenv("LOAD_USER_PASSWORD", "LoadTest123!"),
        "role": UserRole.USER,
    },
    {
        "email": os.getenv("LOAD_ADMIN_EMAIL", "loadtest-admin@example.com"),
        "password": os.getenv("LOAD_ADMIN_PASSWORD", "LoadTest123!"),
        "role": UserRole.ADMIN,
    },
]


async def main() -> None:
    engine = create_async_engine(_DB_URL, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        async with session.begin():
            await _upsert_users(session)

    await engine.dispose()
    print("Done.")


async def _upsert_users(session: AsyncSession) -> None:
    for spec in _USERS:
        existing = await session.scalar(
            select(User).where(User.email == spec["email"])
        )
        if existing:
            print(f"  already exists: {spec['email']}")
            continue
        user = User(
            email=spec["email"],
            hashed_password=hash_password(spec["password"]),
            role=spec["role"],
            is_active=True,
        )
        session.add(user)
        print(f"  created: {spec['email']} ({spec['role'].value})")


if __name__ == "__main__":
    asyncio.run(main())
