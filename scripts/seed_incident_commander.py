"""
Seed the `incident-commander` service account + mint a scoped token.

The agent (github.com/kudratsingh/incident-commander) authenticates as
this principal on every MCP call. Run this once against a running
platform stack; the plaintext token is printed to stdout exactly once
— paste it into the agent's `.env` as PLATFORM_TOKEN (the name the
commander's `Settings.platform_token` reads) and never commit it.

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
    SA_TTL_DAYS      token time-to-live in days, 1-365 (same bound as the
                     API's own mint endpoint). Unset for the platform
                     default (90). 0, a negative, or a non-number exits
                     non-zero without minting — 0 used to be read as
                     "unset" and quietly mint the 90-day default.
    SA_REPLACE_SCOPES  set to 1 to REPLACE an existing account's scopes
                     with SA_SCOPES verbatim instead of merging (see
                     `_ensure_service_account`). Off by default.

Idempotent: re-running finds the existing SA, merges SA_SCOPES into the
scopes it already holds (never narrowing them), and mints a fresh token.
The old token stays valid until its expiry — revoke via the admin
endpoint or the admin UI if you need to rotate.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import timedelta

# Allow running from project root without installing the package, and
# put this script's own dir on the path so `eval_safety` resolves.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import eval_safety  # type: ignore[import-not-found]  # noqa: E402
from app.core.scopes import Scope, validate_scopes  # noqa: E402
from app.core.tenant_scope import platform_session_factory  # noqa: E402
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
# Deliberate-narrowing escape hatch. Default off: the seeder must never
# silently drop a grant the live account already holds (D-01).
_REPLACE_SCOPES = os.getenv("SA_REPLACE_SCOPES", "").strip().lower() in {
    "1",
    "true",
    "yes",
}


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
    """Return (service_account, created). Idempotent.

    On re-run against an existing SA the requested scopes are UNIONED
    with the ones the account already holds — seeding is additive and
    never removes a grant. `ServiceAccountService.update_scopes`, which
    this calls, has replace semantics (its docstring: "Replace the
    account's scope set"), so passing SA_SCOPES straight through used to
    down-scope the live 4-scope incident-commander account to the two
    default read scopes on every `make seed-incident-commander`, and the
    token minted moments later in the same transaction inherited the
    narrowed set (D-01).

    Widening still works: requesting scopes the account lacks — e.g.
    adding `chaos:invoke` + `actions:execute` onto the existing account
    rather than recreating it, which would invalidate every outstanding
    token — adds them.

    Deliberate narrowing is the escape hatch, not the default: with
    SA_REPLACE_SCOPES=1 the requested list is passed verbatim and the
    scopes being removed are named on stderr. (Ad-hoc revocation is
    better done through the admin API, which is replace-by-design.)

    `update_scopes` itself is a no-op when the sorted scope lists match,
    so idempotent re-runs stay audit-quiet."""
    sa_repo = ServiceAccountRepository(session)
    service = ServiceAccountService(
        sa_repo,
        ServiceAccountTokenRepository(session),
        AuditRepository(session),
    )

    existing = await sa_repo.get_by_name(tenant_id, name)
    if existing is not None:
        current = set(existing.scopes)
        if _REPLACE_SCOPES:
            target = list(scopes)
            removed = sorted(current - set(scopes))
            if removed:
                print(
                    "WARNING: SA_REPLACE_SCOPES=1 — removing scope(s) from "
                    f"{name!r}: {', '.join(removed)}. Tokens already minted "
                    "keep the scopes they carry; revoke them if this is a "
                    "privilege reduction.",
                    file=sys.stderr,
                )
        else:
            target = sorted(current | set(scopes))
        await service.update_scopes(
            service_account=existing,
            scopes=target,
            updated_by_user_id=None,
        )
        return existing, False

    sa = await service.create_service_account(
        tenant_id=tenant_id,
        name=name,
        scopes=scopes,
        created_by_user_id=None,
    )
    return sa, True


_TTL_MIN_DAYS = 1
_TTL_MAX_DAYS = 365


def _parse_ttl_days(raw: str | None) -> int | None:
    """`SA_TTL_DAYS` -> an int in [1, 365], or `None` when unset.

    Unset means "platform default" (90 days). Everything else must be a
    number in the same range the API's own `MintTokenRequest.ttl_days`
    accepts (`ge=1, le=365`) — a script that mints the identical
    credential should not accept values the endpoint rejects.

    The value this exists for is `0`. `int(env) if env else None`
    parsed it to `0`, and `timedelta(days=ttl) if ttl else None` then
    read that `0` as falsy and minted the **90-day default** — an
    operator who asked for the shortest possible lifetime got the
    longest one, silently, with the plaintext token printed as if
    nothing had happened (WO-R2-19). Negatives were worse in a quieter
    way: `-5` is truthy, so it minted a token that was already expired.

    Raises `ValueError` with an actionable message; `main()` turns that
    into stderr + a non-zero exit rather than a traceback."""
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    try:
        days = int(text)
    except ValueError:
        raise ValueError(
            f"SA_TTL_DAYS must be a whole number of days, got {text!r}. "
            f"Valid range is {_TTL_MIN_DAYS}-{_TTL_MAX_DAYS}; unset it for "
            "the platform default (90)."
        ) from None
    if not (_TTL_MIN_DAYS <= days <= _TTL_MAX_DAYS):
        detail = (
            "a token with a zero-length lifetime cannot be used for "
            "anything, and this used to be read as 'unset' and mint the "
            "90-day default instead"
            if days == 0
            else "out of range"
        )
        raise ValueError(
            f"SA_TTL_DAYS={days} is invalid ({detail}). Valid range is "
            f"{_TTL_MIN_DAYS}-{_TTL_MAX_DAYS}; unset it for the platform "
            "default (90). No token was minted."
        )
    return days


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
    # `is not None`, not truthiness: `_parse_ttl_days` already rejects 0,
    # but truthiness is what turned an explicit 0 into the 90-day default
    # in the first place, and it should not be the spelling here either.
    ttl = timedelta(days=ttl_days) if ttl_days is not None else None
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
    print(f"PLATFORM_TOKEN={plaintext}")
    print()
    print("Paste that line into the incident-commander repo's .env — that")
    print("is the name it reads (Settings.platform_token). Never commit it.")
    print()


async def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Seed the incident-commander service account and mint a "
            "scoped bearer token. Refuses to run against "
            "ENVIRONMENT=production or against any DATABASE_URL other "
            "than the configured one."
        )
    )
    parser.add_argument(
        "--i-know-what-im-doing",
        dest="allow_target_mismatch",
        action="store_true",
        help=(
            "Mint against a DATABASE_URL that is not the configured one. "
            "Does not override the production check."
        ),
    )
    args = parser.parse_args()

    scopes = _parse_scopes(_SCOPES_ENV)
    # Validate before the gate's DB work and before anything is minted,
    # so a bad TTL costs nothing and prints one line.
    try:
        ttl_days = _parse_ttl_days(_TTL_DAYS_ENV)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    # This script writes a live credential; gate it on the target the
    # same way the destructive scripts are gated (WO-R2-19).
    eval_safety.refuse_unsafe_target(
        script="seed_incident_commander.py",
        database_url=_DB_URL,
        allow_target_mismatch=args.allow_target_mismatch,
    )
    print(eval_safety.describe_target(_DB_URL))

    engine = create_async_engine(_DB_URL, echo=False)
    # Platform (cross-tenant) scope: this script touches many tenants'
    # rows and sets no `app.tenant_id`. Since WO-R2-129 that is refused
    # rather than silently admitted, and it runs as `incident_app`
    # (docker-compose `app` service) — a non-owner role with no
    # BYPASSRLS — so the declaration is what keeps it working. ADR 0026.
    factory = platform_session_factory(engine)

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
