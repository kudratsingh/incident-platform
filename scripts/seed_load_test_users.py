"""
Create the two load-test accounts locustfile.py expects, with the stack running:
`python scripts/seed_load_test_users.py`.

Optional env vars (defaults match locustfile.py): `DATABASE_URL`, `LOAD_USER_EMAIL`,
`LOAD_USER_PASSWORD`, `LOAD_ADMIN_EMAIL`, `LOAD_ADMIN_PASSWORD`.

One account is `role=admin` with a repo-published default password, so this goes through the
`eval_safety` gate (WO-R2-19): it refuses on `ENVIRONMENT=production` or a `DATABASE_URL` other
than the configured one, prints the target first, and takes `--i-know-what-im-doing` for a
deliberate cross-stack seed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# backend/ and this dir on sys.path: `app` and `eval_safety`.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import eval_safety  # type: ignore[import-not-found]  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.models.tenant import DEFAULT_TENANT_ID  # noqa: E402
from app.models.user import User  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create the two load-test accounts locustfile.py expects "
            "(one of them role=admin, with a repo-published default "
            "password). Refuses to run against ENVIRONMENT=production or "
            "against any DATABASE_URL other than the configured one."
        )
    )
    parser.add_argument(
        "--i-know-what-im-doing",
        dest="allow_target_mismatch",
        action="store_true",
        help=(
            "Write the load-test accounts even though DATABASE_URL is not "
            "the configured one. Does not override the production check."
        ),
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    # Before the engine exists, so a refusal connects to nothing.
    eval_safety.refuse_unsafe_target(
        script="seed_load_test_users.py",
        database_url=_DB_URL,
        allow_target_mismatch=args.allow_target_mismatch,
    )
    # So an operator can see where the admin account is going.
    print(eval_safety.describe_target(_DB_URL))

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
            # users.tenant_id is NOT NULL since Phase 12; seeded into
            # DEFAULT_TENANT_ID like every other bootstrap path.
            tenant_id=DEFAULT_TENANT_ID,
            email=spec["email"],
            hashed_password=hash_password(spec["password"]),
            role=spec["role"],
            is_active=True,
        )
        session.add(user)
        print(f"  created: {spec['email']} ({spec['role'].value})")


if __name__ == "__main__":
    asyncio.run(main())
