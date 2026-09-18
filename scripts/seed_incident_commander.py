"""
Seed the eval's two service accounts and mint one scoped token for each.

**Two principals, not one** (O-4, 2026-09-15). The platform withholds the `chaos.` audit stream
from any principal without `chaos:invoke`, and that filter is inert if one token carries both:

  - `incident-commander` — the agent under test: reads plus Tier-1 actions, **never**
    `chaos:invoke` (this script removes the grant if it finds it, on stderr). Printed as
    `PLATFORM_TOKEN`, the name the commander's `Settings.platform_token` reads.
  - `incident-commander-chaos` — the evaluator: `chaos:invoke` plus the two read scopes, so it
    can seed a world, verify it and tear it down. Printed as `PLATFORM_CHAOS_TOKEN`.

Neither plaintext is printed again. Paste both into the commander's `.env`; never commit either.

Usage (with the stack running): `python scripts/seed_incident_commander.py`. Optional env vars:
`DATABASE_URL` (compose value), `SA_NAME` (incident-commander), `SA_CHAOS_NAME`
(incident-commander-chaos), `SA_TENANT_SLUG` (default), `SA_SCOPES`
(telemetry:read,incidents:read — `chaos:invoke` here is refused, it belongs to the chaos account),
`SA_CHAOS_SCOPES` (those two plus chaos:invoke; no `actions:execute`, remediation is the agent's
job), `SA_TTL_DAYS` (1-365, the API's own bound; unset for the platform default of 90 — 0,
negative or non-numeric exits without minting), `SA_REPLACE_SCOPES` (1 to REPLACE an account's
scopes verbatim instead of merging).

Idempotent: a re-run merges requested scopes into what each account holds (never narrowing, except
`chaos:invoke` on the agent) and mints a fresh token; old tokens stay valid until they expire.
`actions:execute` is added to the live agent account by the eval bootstrap; `actions:propose`
goes to nobody, because the approvals subsystem is unbuilt.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import timedelta

# backend/ and this dir on sys.path: `app` and `eval_safety`.
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
_CHAOS_SA_NAME = os.getenv("SA_CHAOS_NAME", "incident-commander-chaos")
_TENANT_SLUG = os.getenv("SA_TENANT_SLUG", "default")
_SCOPES_ENV = os.getenv(
    "SA_SCOPES",
    f"{Scope.TELEMETRY_READ.value},{Scope.INCIDENTS_READ.value}",
)
_CHAOS_SCOPES_ENV = os.getenv(
    "SA_CHAOS_SCOPES",
    ",".join(
        (
            Scope.TELEMETRY_READ.value,
            Scope.INCIDENTS_READ.value,
            Scope.CHAOS_INVOKE.value,
        )
    ),
)
_TTL_DAYS_ENV = os.getenv("SA_TTL_DAYS")
# Deliberate-narrowing escape hatch, off by default: never drop a grant silently (D-01).
_REPLACE_SCOPES = os.getenv("SA_REPLACE_SCOPES", "").strip().lower() in {
    "1",
    "true",
    "yes",
}


# Scopes the AGENT account must never carry (O-4): `operator_audit.hidden_audit_action_prefixes`
# hides `chaos.` rows from principals without it, so one all-scope token makes the filter inert.
_AGENT_FORBIDDEN_SCOPES: frozenset[str] = frozenset({Scope.CHAOS_INVOKE.value})


def _parse_scopes(raw: str, *, forbidden: frozenset[str] = frozenset()) -> list[str]:
    """Turn a comma-separated scope string into a checked list, stopping if it
    names a scope this principal is not allowed to hold."""
    scopes = [s.strip() for s in raw.split(",") if s.strip()]
    try:
        validate_scopes(scopes)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    refused = sorted(set(scopes) & forbidden)
    if refused:
        # Refused, not quietly dropped: a silently narrower token later is worse.
        raise SystemExit(
            f"error: scope(s) {refused} must not be granted to the agent "
            f"account {_SA_NAME!r}. The chaos account "
            f"({_CHAOS_SA_NAME!r}, SA_CHAOS_SCOPES) is the principal that "
            "holds them — an agent token carrying chaos:invoke can read "
            "the chaos audit stream, which is the leak the split closes "
            "(owner decision O-4)."
        )
    return scopes


async def _resolve_tenant(
    session: AsyncSession, slug: str
) -> Tenant:
    """The tenant to seed into, or a clear exit explaining why there is none."""
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
    forbidden_scopes: frozenset[str] = frozenset(),
) -> tuple[object, bool]:
    """Return (service_account, created); idempotent, and requested scopes are UNIONED with what
    the account holds, so seeding never drops a grant silently (D-01) — `SA_REPLACE_SCOPES=1`
    replaces verbatim, `forbidden_scopes` (the agent's `chaos:invoke`, O-4) narrows by default,
    and both name the removals on stderr."""
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
        stripped = sorted(set(target) & forbidden_scopes)
        if stripped:
            target = sorted(set(target) - forbidden_scopes)
            print(
                f"NOTE: removing scope(s) from {name!r}: "
                f"{', '.join(stripped)} — this principal is the agent under "
                "test and must not hold them (owner decision O-4). Tokens "
                "minted before now keep the scopes they carry: re-paste the "
                "PLATFORM_TOKEN printed below, and revoke the old one if it "
                "is still in use.",
                file=sys.stderr,
            )
        await service.update_scopes(
            service_account=existing,
            scopes=target,
            updated_by_user_id=None,
        )
        return existing, False

    sa = await service.create_service_account(
        tenant_id=tenant_id,
        name=name,
        scopes=sorted(set(scopes) - forbidden_scopes),
        created_by_user_id=None,
    )
    return sa, True


_TTL_MIN_DAYS = 1
_TTL_MAX_DAYS = 365


def _parse_ttl_days(raw: str | None) -> int | None:
    """`SA_TTL_DAYS` -> an int in [1, 365], or `None` when unset (platform default, 90 days).

    Same range as the API's `MintTokenRequest.ttl_days`. `0` is rejected, not read as unset — it
    used to mint the 90-day default silently (WO-R2-19). Raises `ValueError`; `main()` exits."""
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
    """Mint one bearer token for the account and return the plaintext — the
    only moment it is ever readable."""
    service = ServiceAccountService(
        ServiceAccountRepository(session),
        ServiceAccountTokenRepository(session),
        AuditRepository(session),
    )
    # `is not None`, not truthiness — truthiness turned an explicit 0 into the 90-day default.
    ttl = timedelta(days=ttl_days) if ttl_days is not None else None
    _, plaintext = await service.mint_token(
        service_account=service_account,
        scopes=None,  # inherit full account scope set
        ttl=ttl,
        minted_by_user_id=None,
    )
    return plaintext


#: Env-var label and role per minted token — the commander's own `.env` names.
#: D-11: the banner once printed `PLATFORM_MCP_TOKEN`, a name nothing read.
AGENT_TOKEN_LABEL = "PLATFORM_TOKEN"
CHAOS_TOKEN_LABEL = "PLATFORM_CHAOS_TOKEN"
AGENT_TOKEN_ROLE = "the agent under test — reads + Tier-1 actions, no chaos:invoke"
CHAOS_TOKEN_ROLE = "the evaluator — seeds, verifies and resets the chaos world"


def _print_banner(accounts: list[tuple[str, str, list[str], bool, str]]) -> None:
    """Human-readable summary for both principals.

    `accounts` is `(label, name, scopes, created, plaintext)` in paste order; the labels are
    explicit because swapping the two tokens fails silently either way.
    """
    print()
    for label, name, scopes, created, _plaintext in accounts:
        state = "created" if created else "already existed (fresh token minted)"
        role = AGENT_TOKEN_ROLE if label == AGENT_TOKEN_LABEL else CHAOS_TOKEN_ROLE
        print(f"service account: {name}  [{state}]")
        print(f"  role:          {role}")
        print(f"  scopes:        {', '.join(scopes)}")
        print(f"  .env name:     {label}")
        print()
    print("+---------------------------------------------------------------+")
    print("|  CAPTURE THESE TOKENS NOW — neither is printed again.         |")
    print("+---------------------------------------------------------------+")
    print()
    for label, _name, _scopes, _created, plaintext in accounts:
        print(f"{label}={plaintext}")
    print()
    print("Paste both lines into the incident-commander repo's .env. They are")
    print("two different principals on purpose: the platform withholds the")
    print("chaos audit stream from any principal that cannot fire chaos, so")
    print("an agent token carrying chaos:invoke can read what was injected.")
    print("Never commit either value.")
    print()


async def main() -> None:
    """Check the target is safe, bring both accounts to their declared scopes,
    mint a token for each and print the pair once."""
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Seed the two eval service accounts — the agent and the chaos "
            "runner — and mint one scoped bearer token for each. Refuses "
            "to run against ENVIRONMENT=production or against any "
            "DATABASE_URL other than the configured one."
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

    scopes = _parse_scopes(_SCOPES_ENV, forbidden=_AGENT_FORBIDDEN_SCOPES)
    chaos_scopes = _parse_scopes(_CHAOS_SCOPES_ENV)
    # Validate before the gate's DB work and before anything is minted.
    try:
        ttl_days = _parse_ttl_days(_TTL_DAYS_ENV)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    # This writes a live credential, so gate the target too (WO-R2-19).
    eval_safety.refuse_unsafe_target(
        script="seed_incident_commander.py",
        database_url=_DB_URL,
        allow_target_mismatch=args.allow_target_mismatch,
    )
    print(eval_safety.describe_target(_DB_URL))

    engine = create_async_engine(_DB_URL, echo=False)
    # Platform (cross-tenant) scope: this script sets no `app.tenant_id`, which ADR 0026
    # refuses, and runs as the non-owner `incident_app` role with no BYPASSRLS.
    factory = platform_session_factory(engine)

    # One transaction for both: half a run leaves a token with no partner.
    async with factory() as session:
        async with session.begin():
            tenant = await _resolve_tenant(session, _TENANT_SLUG)
            sa, created = await _ensure_service_account(
                session,
                tenant.id,
                _SA_NAME,
                scopes,
                forbidden_scopes=_AGENT_FORBIDDEN_SCOPES,
            )
            plaintext = await _mint(session, sa, ttl_days)
            chaos_sa, chaos_created = await _ensure_service_account(
                session, tenant.id, _CHAOS_SA_NAME, chaos_scopes
            )
            chaos_plaintext = await _mint(session, chaos_sa, ttl_days)

    await engine.dispose()
    _print_banner(
        [
            (
                AGENT_TOKEN_LABEL,
                _SA_NAME,
                list(sa.scopes),  # type: ignore[attr-defined]
                created,
                plaintext,
            ),
            (
                CHAOS_TOKEN_LABEL,
                _CHAOS_SA_NAME,
                list(chaos_sa.scopes),  # type: ignore[attr-defined]
                chaos_created,
                chaos_plaintext,
            ),
        ]
    )


if __name__ == "__main__":
    asyncio.run(main())
