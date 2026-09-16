"""
Seed the eval's two service accounts + mint one scoped token for each.

**Two principals, not one** (owner decision O-4, 2026-09-15). The agent
under test and the evaluator that seeds its faults are different
principals, because the platform withholds the `chaos.` audit stream from
anyone without `chaos:invoke` and that separation is worth nothing if one
token carries both roles:

  - `incident-commander` — the agent. Reads the platform and executes
    Tier-1 actions. **Never holds `chaos:invoke`**, and this script
    removes the grant if it finds it, so the agent cannot fire a fault,
    and cannot read that one was fired. Printed as `PLATFORM_TOKEN`, the
    name the commander's `Settings.platform_token` reads.
  - `incident-commander-chaos` — the evaluator / runner. Holds
    `chaos:invoke` plus the two read scopes, so it can seed a world,
    verify what it seeded, and tear it down. Printed as
    `PLATFORM_CHAOS_TOKEN`.

Before the split, the runner handed the agent its own 4-scope token on
every live remediation run, so `list_audit_events` answered "who broke
this?" with the hook name and its arguments — the divergence report's
row G3. A filter keyed on the scope is inert while one token holds
every scope; that is why the token split and the filter ship together.

Neither plaintext is printed anywhere again. Paste both into the
commander's `.env`; never commit either.

Scopes: the agent's default is `telemetry:read` (consumer lag, health,
deploy history) + `incidents:read` (DLQ, traces, DAG state, incidents,
audit log); `actions:execute` is added to the live account by the eval
bootstrap, and a re-run keeps whatever it already holds apart from
`chaos:invoke`. `actions:propose` is granted to nobody — the approvals
subsystem is unbuilt.

Usage (with the stack running):

    python scripts/seed_incident_commander.py

Env vars (all optional):

    DATABASE_URL     postgres+asyncpg://... (defaults to compose value)
    SA_NAME          default: incident-commander
    SA_CHAOS_NAME    default: incident-commander-chaos
    SA_TENANT_SLUG   default: default
    SA_SCOPES        comma-separated scopes for the AGENT account
                     (default: telemetry:read,incidents:read).
                     `chaos:invoke` here is refused — it belongs to the
                     chaos account, and accepting it would undo the split.
    SA_CHAOS_SCOPES  comma-separated scopes for the CHAOS account
                     (default: telemetry:read,incidents:read,chaos:invoke).
                     Reads are included so the runner can verify the world
                     it seeded under its own token; `actions:execute` is
                     not, because remediation is the agent's job.
    SA_TTL_DAYS      token time-to-live in days, 1-365 (same bound as the
                     API's own mint endpoint). Applies to both tokens.
                     Unset for the platform default (90). 0, a negative,
                     or a non-number exits non-zero without minting — 0
                     used to be read as "unset" and quietly mint the
                     90-day default.
    SA_REPLACE_SCOPES  set to 1 to REPLACE an existing account's scopes
                     with the requested list verbatim instead of merging
                     (see `_ensure_service_account`). Off by default.

Idempotent: re-running finds each existing SA, merges the requested
scopes into what it already holds (never narrowing — with the single,
deliberate exception of `chaos:invoke` on the agent account, which is
removed and said so on stderr), and mints a fresh token for each. Old
tokens stay valid until they expire — revoke via the admin endpoint or
the admin UI if you need to rotate.
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
# Deliberate-narrowing escape hatch. Default off: the seeder must never
# silently drop a grant the live account already holds (D-01).
_REPLACE_SCOPES = os.getenv("SA_REPLACE_SCOPES", "").strip().lower() in {
    "1",
    "true",
    "yes",
}


# Scopes the AGENT account must never carry. One member, and the whole
# point of the two-account split: the agent may not fire the lab, and the
# `chaos.` audit stream is withheld from principals that cannot
# (`app.services.operator_audit.hidden_audit_action_prefixes`). Removing it
# is the one narrowing this script performs without SA_REPLACE_SCOPES —
# D-01's rule is that a grant is never dropped *silently*, and this one is
# announced on stderr and is the reason the script exists in this shape.
_AGENT_FORBIDDEN_SCOPES: frozenset[str] = frozenset({Scope.CHAOS_INVOKE.value})


def _parse_scopes(raw: str, *, forbidden: frozenset[str] = frozenset()) -> list[str]:
    scopes = [s.strip() for s in raw.split(",") if s.strip()]
    try:
        validate_scopes(scopes)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    refused = sorted(set(scopes) & forbidden)
    if refused:
        # Refused rather than quietly dropped: an operator who asked for
        # this wants a principal the split says cannot exist, and finding
        # out from a silently narrower token later is worse than an exit
        # code now.
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

    `forbidden_scopes` is the one narrowing that happens by default, and
    it is not a relaxation of D-01. The agent account is passed
    `{chaos:invoke}` (O-4): the live account holds that grant today, the
    filter that hides the lab's audit rows keys on it, and a union-only
    seeder could therefore never reach the state the decision asks for.
    The removal is announced on stderr, and outstanding tokens keep the
    scopes they already carry — so the operator is told to re-paste, which
    the banner also says.

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


#: Env-var label per minted token, and the role each one plays. The labels
#: are the commander's own `.env` names — `PLATFORM_TOKEN` is what
#: `Settings.platform_token` reads, `PLATFORM_CHAOS_TOKEN` is what the
#: runner uses to seed and reset (D-11 is the reason these are pinned by a
#: test: the banner once printed `PLATFORM_MCP_TOKEN`, a name nothing read).
AGENT_TOKEN_LABEL = "PLATFORM_TOKEN"
CHAOS_TOKEN_LABEL = "PLATFORM_CHAOS_TOKEN"
AGENT_TOKEN_ROLE = "the agent under test — reads + Tier-1 actions, no chaos:invoke"
CHAOS_TOKEN_ROLE = "the evaluator — seeds, verifies and resets the chaos world"


def _print_banner(accounts: list[tuple[str, str, list[str], bool, str]]) -> None:
    """Human-readable summary for both principals.

    `accounts` is `(label, name, scopes, created, plaintext)` in the order
    they should be pasted. The two `KEY=value` lines are the only thing the
    operator needs to capture; everything else is context. Printing both
    under explicit labels is the point — an operator who pasted one token
    into both variables would have a runner that cannot seed, or an agent
    that can read the lab, and neither failure names itself.
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

    # Both accounts and both tokens in ONE transaction: a run that created
    # the agent principal and then failed would leave an operator holding a
    # token whose partner does not exist, and the fix for that is a rerun
    # they have no way to know they need.
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
