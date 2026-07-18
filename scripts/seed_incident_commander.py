"""
Seed the `incident-commander` service account + mint a scoped token.

The agent (github.com/kudratsingh/incident-commander) authenticates as
this principal on every MCP call. Run this once against a running
platform stack; the plaintext token is printed to stdout exactly once
— paste it into the agent's `.env` as PLATFORM_MCP_TOKEN and never
commit it.

Scopes granted by default match the agent's Phase 0–3 needs:
`telemetry:read` (consumer lag, health, deploy history) and
`incidents:read` (DLQ, traces, DAG state, incidents, audit log).
Wave 3 write scopes (`actions:propose`, `actions:execute`) are
deliberately NOT included — those are minted separately when the
agent reaches Phase 6.

Usage (with the stack running):

    python scripts/seed_incident_commander.py

Env vars (all optional):

    DATABASE_URL     postgres+asyncpg://... (defaults to compose value)
    SA_NAME          default: incident-commander
    SA_TENANT_SLUG   default: default
    SA_SCOPES        comma-separated (default: telemetry:read,incidents:read)
    SA_TTL_DAYS      token time-to-live (default: platform default, 90)

Idempotent: re-running finds the existing SA and mints a fresh token.
The old token stays valid until its expiry — revoke via the admin
endpoint or the admin UI if you need to rotate.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import timedelta

# Allow running from project root without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.core.scopes import Scope, validate_scopes  # noqa: E402
from app.models.tenant import Tenant  # noqa: E402
from app.repositories.audit import AuditRepository  # noqa: E402
from app.repositories.service_account import (  # noqa: E402
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.repositories.tenant import TenantRepository  # noqa: E402
from app.services.service_account import ServiceAccountService  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/incident_platform",
)
_SA_NAME = os.getenv("SA_NAME", "incident-commander")
_TENANT_SLUG = os.getenv("SA_TENANT_SLUG", "default")
_SCOPES_ENV = os.getenv(
    "SA_SCOPES",
    f"{Scope.TELEMETRY_READ.value},{Scope.INCIDENTS_READ.value}",
)
_TTL_DAYS_ENV = os.getenv("SA_TTL_DAYS")


def _parse_scopes(raw: str) -> list[str]:
    scopes = [s.strip() for s in raw.split(",") if s.strip()]
    try:
        validate_scopes(scopes)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    return scopes


async def _resolve_tenant(
    session: AsyncSession, slug: str
) -> Tenant:
    tenant = await TenantRepository(session).get_by_slug(slug)
    if tenant is None:
        raise SystemExit(
            f"error: tenant slug {slug!r} not found. "
            "Run `alembic upgrade head` to seed the default tenant, or "
            "pass SA_TENANT_SLUG=<existing slug>."
        )
    if not tenant.is_active:
        raise SystemExit(f"error: tenant {slug!r} is inactive")
    return tenant


async def _ensure_service_account(
    session: AsyncSession,
    tenant_id,  # type: ignore[no-untyped-def]
    name: str,
    scopes: list[str],
) -> tuple[object, bool]:
    """Return (service_account, created). Idempotent — an existing SA
    is returned as-is; scopes on it are NOT modified (that's an admin
    operation, not a seed one)."""
    sa_repo = ServiceAccountRepository(session)
    existing = await sa_repo.get_by_name(tenant_id, name)
    if existing is not None:
        return existing, False

    service = ServiceAccountService(
        sa_repo,
        ServiceAccountTokenRepository(session),
        AuditRepository(session),
    )
    sa = await service.create_service_account(
        tenant_id=tenant_id,
        name=name,
        scopes=scopes,
        created_by_user_id=None,
    )
    return sa, True


async def _mint(
    session: AsyncSession,
    service_account,  # type: ignore[no-untyped-def]
    ttl_days: int | None,
) -> str:
    service = ServiceAccountService(
        ServiceAccountRepository(session),
        ServiceAccountTokenRepository(session),
        AuditRepository(session),
    )
    ttl = timedelta(days=ttl_days) if ttl_days else None
    _, plaintext = await service.mint_token(
        service_account=service_account,
        scopes=None,  # inherit full account scope set
        ttl=ttl,
        minted_by_user_id=None,
    )
    return plaintext


def _print_banner(name: str, scopes: list[str], created: bool, plaintext: str) -> None:
    """Human-readable summary. The plaintext line is the only thing the
    operator needs to capture — the rest is context."""
    banner = "created" if created else "already existed (fresh token minted)"
    print()
    print(f"service account: {name}  [{banner}]")
    print(f"scopes:          {', '.join(scopes)}")
    print()
    print("+---------------------------------------------------------------+")
    print("|  CAPTURE THIS TOKEN NOW — it is not printed anywhere again.  |")
    print("+---------------------------------------------------------------+")
    print()
    print(f"PLATFORM_MCP_TOKEN={plaintext}")
    print()
    print("Paste that line into the agent repo's .env (or wherever it")
    print("reads PLATFORM_MCP_TOKEN from). Never commit it.")
    print()


async def main() -> None:
    scopes = _parse_scopes(_SCOPES_ENV)
    ttl_days = int(_TTL_DAYS_ENV) if _TTL_DAYS_ENV else None

    engine = create_async_engine(_DB_URL, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        async with session.begin():
            tenant = await _resolve_tenant(session, _TENANT_SLUG)
            sa, created = await _ensure_service_account(
                session, tenant.id, _SA_NAME, scopes
            )
            plaintext = await _mint(session, sa, ttl_days)

    await engine.dispose()
    _print_banner(_SA_NAME, list(sa.scopes), created, plaintext)  # type: ignore[attr-defined]


if __name__ == "__main__":
    asyncio.run(main())
