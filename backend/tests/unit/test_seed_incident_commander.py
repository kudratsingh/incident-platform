"""Tests for `scripts/seed_incident_commander.py` (D-01, D-11).

D-01: the seeder called `ServiceAccountService.update_scopes`, which
REPLACES the scope set. A default re-run (`make seed-incident-commander`
with no SA_SCOPES) therefore silently down-scoped the live 4-scope
`incident-commander` account to the two default read scopes, and the
token minted in the same transaction inherited the narrowed set. These
tests pin the union semantics plus the deliberate-narrowing escape
hatch.

D-11: the banner printed `PLATFORM_MCP_TOKEN=`, but the commander reads
`PLATFORM_TOKEN` (incident-commander `src/incident_commander/config.py`
declares `platform_token: SecretStr` with no env prefix;
`.env.example` ships `PLATFORM_TOKEN=`).

Import-guarded via the sys.path pattern in `test_eval_reset.py` — the
scripts/ dir isn't a package on disk.
"""

from __future__ import annotations

import importlib
import os
import sys
from types import ModuleType

import pytest
from app.core.scopes import Scope
from app.repositories.service_account import ServiceAccountRepository
from sqlalchemy.ext.asyncio import AsyncSession

# The scripts/ dir isn't a package on disk; make it importable.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_SCRIPTS = os.path.join(_ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

_SA_NAME = "incident-commander"

# The SA_SCOPES default baked into the script (and into
# `make seed-incident-commander`, which passes no SA_SCOPES).
_DEFAULT_SCOPES = [Scope.TELEMETRY_READ.value, Scope.INCIDENTS_READ.value]
_FULL_SCOPES = [
    Scope.TELEMETRY_READ.value,
    Scope.INCIDENTS_READ.value,
    Scope.ACTIONS_EXECUTE.value,
    Scope.CHAOS_INVOKE.value,
]


def _seed_module() -> ModuleType:
    return importlib.import_module("seed_incident_commander")


async def _existing_sa(
    session: AsyncSession, tenant_id: object, scopes: list[str]
) -> object:
    return await ServiceAccountRepository(session).create(
        tenant_id=tenant_id,
        name=_SA_NAME,
        scopes=list(scopes),
        is_active=True,
        created_by_user_id=None,
    )


# ---------------------------------------------------------------------------
# D-01 — scope merge
# ---------------------------------------------------------------------------


async def test_default_rerun_never_removes_write_or_chaos_scopes(
    db_session: AsyncSession, default_tenant
) -> None:
    """THE assertion that would have caught D-01.

    A live 4-scope account re-seeded with the SA_SCOPES default keeps
    its write/chaos grants.

    This is the helper's own union semantics, with no `forbidden_scopes`
    passed — still the behaviour every caller but one relies on. The agent
    path (WO-R3-187) passes `{chaos:invoke}` and does narrow, which is
    tested separately below; D-01's rule is that a grant is never dropped
    *silently*, not that one can never be dropped."""
    seed = _seed_module()
    sa = await _existing_sa(db_session, default_tenant.id, _FULL_SCOPES)

    returned, created = await seed._ensure_service_account(
        db_session, default_tenant.id, _SA_NAME, list(_DEFAULT_SCOPES)
    )

    assert created is False
    assert returned is sa
    assert {Scope.ACTIONS_EXECUTE.value, Scope.CHAOS_INVOKE.value} <= set(sa.scopes)
    assert sorted(sa.scopes) == sorted(_FULL_SCOPES)


async def test_union_still_widens_an_existing_account(
    db_session: AsyncSession, default_tenant
) -> None:
    """Merging is a union, not a freeze: requesting more still widens."""
    seed = _seed_module()
    sa = await _existing_sa(db_session, default_tenant.id, _DEFAULT_SCOPES)

    await seed._ensure_service_account(
        db_session, default_tenant.id, _SA_NAME, list(_FULL_SCOPES)
    )

    assert sorted(sa.scopes) == sorted(_FULL_SCOPES)


async def test_replace_flag_narrows_verbatim(
    db_session: AsyncSession,
    default_tenant,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SA_REPLACE_SCOPES=1 is the deliberate-narrowing escape hatch."""
    seed = _seed_module()
    # Env vars are read at module import; patch the module-level constant.
    monkeypatch.setattr(seed, "_REPLACE_SCOPES", True)
    sa = await _existing_sa(db_session, default_tenant.id, _FULL_SCOPES)

    await seed._ensure_service_account(
        db_session, default_tenant.id, _SA_NAME, list(_DEFAULT_SCOPES)
    )

    assert sorted(sa.scopes) == sorted(_DEFAULT_SCOPES)
    # And it says out loud which grants it just took away.
    stderr = capsys.readouterr().err
    assert Scope.CHAOS_INVOKE.value in stderr
    assert Scope.ACTIONS_EXECUTE.value in stderr


async def test_creates_account_with_requested_scopes_when_absent(
    db_session: AsyncSession, default_tenant
) -> None:
    """First run still creates the account with exactly what was asked."""
    seed = _seed_module()

    sa, created = await seed._ensure_service_account(
        db_session, default_tenant.id, _SA_NAME, list(_DEFAULT_SCOPES)
    )

    assert created is True
    assert sorted(sa.scopes) == sorted(_DEFAULT_SCOPES)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# O-4 / WO-R3-187 — two accounts, and the agent never holds chaos:invoke
# ---------------------------------------------------------------------------


async def test_agent_rerun_removes_chaos_invoke(
    db_session: AsyncSession,
    default_tenant,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """THE assertion for the split.

    The live agent account holds `chaos:invoke` today (divergence report
    G3), and the audit filter this work order lands keys on that scope —
    so a union-only seeder could never reach the state owner decision O-4
    asks for. Re-seeding the agent account drops the grant, keeps every
    other one, and says so on stderr.
    """
    seed = _seed_module()
    sa = await _existing_sa(db_session, default_tenant.id, _FULL_SCOPES)

    returned, created = await seed._ensure_service_account(
        db_session,
        default_tenant.id,
        _SA_NAME,
        list(_DEFAULT_SCOPES),
        forbidden_scopes=seed._AGENT_FORBIDDEN_SCOPES,
    )

    assert created is False
    assert returned is sa
    assert Scope.CHAOS_INVOKE.value not in sa.scopes
    assert Scope.ACTIONS_EXECUTE.value in sa.scopes, (
        "only chaos:invoke is taken away — the agent still executes Tier-1"
    )
    stderr = capsys.readouterr().err
    assert Scope.CHAOS_INVOKE.value in stderr
    assert "O-4" in stderr, "the removal has to name the decision behind it"


async def test_agent_create_path_cannot_be_talked_into_chaos_invoke(
    db_session: AsyncSession, default_tenant
) -> None:
    """Belt and braces: even handed the scope directly, the create path
    strips it. `_parse_scopes` refuses SA_CHAOS_SCOPES-shaped input for the
    agent, but a future caller of this helper should not be able to
    reintroduce the leak by passing a list."""
    seed = _seed_module()

    sa, created = await seed._ensure_service_account(
        db_session,
        default_tenant.id,
        _SA_NAME,
        [*_DEFAULT_SCOPES, Scope.CHAOS_INVOKE.value],
        forbidden_scopes=seed._AGENT_FORBIDDEN_SCOPES,
    )

    assert created is True
    assert Scope.CHAOS_INVOKE.value not in sa.scopes  # type: ignore[attr-defined]


def test_sa_scopes_refuses_chaos_invoke_for_the_agent() -> None:
    """`SA_SCOPES=…,chaos:invoke` exits rather than minting a narrower
    token than it was asked for — the operator wants a principal the split
    says cannot exist, and should hear that now."""
    seed = _seed_module()

    with pytest.raises(SystemExit) as exc:
        seed._parse_scopes(
            f"{Scope.INCIDENTS_READ.value},{Scope.CHAOS_INVOKE.value}",
            forbidden=seed._AGENT_FORBIDDEN_SCOPES,
        )

    message = str(exc.value)
    assert Scope.CHAOS_INVOKE.value in message
    assert "SA_CHAOS_SCOPES" in message, "say where the scope does belong"


def test_the_two_accounts_are_different_principals() -> None:
    """Different names and complementary scope sets — one may fire the lab
    and the other may not, which is the whole mechanism."""
    seed = _seed_module()

    assert seed._CHAOS_SA_NAME != seed._SA_NAME
    agent = set(seed._parse_scopes(seed._SCOPES_ENV))
    chaos = set(seed._parse_scopes(seed._CHAOS_SCOPES_ENV))
    assert Scope.CHAOS_INVOKE.value not in agent
    assert Scope.CHAOS_INVOKE.value in chaos
    # The runner verifies the world it seeded under its own token, so it
    # needs the reads; it never remediates, so it must not execute.
    assert {Scope.TELEMETRY_READ.value, Scope.INCIDENTS_READ.value} <= chaos
    assert Scope.ACTIONS_EXECUTE.value not in chaos


# ---------------------------------------------------------------------------
# D-11 — the banner names the env vars the commander actually reads
# ---------------------------------------------------------------------------


def test_banner_prints_both_labelled_tokens(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed = _seed_module()

    seed._print_banner(
        [
            (
                seed.AGENT_TOKEN_LABEL,
                _SA_NAME,
                list(_DEFAULT_SCOPES),
                False,
                "sa_agent123",
            ),
            (
                seed.CHAOS_TOKEN_LABEL,
                seed._CHAOS_SA_NAME,
                [Scope.CHAOS_INVOKE.value],
                True,
                "sa_chaos456",
            ),
        ]
    )

    out = capsys.readouterr().out
    assert "PLATFORM_TOKEN=sa_agent123" in out
    assert "PLATFORM_CHAOS_TOKEN=sa_chaos456" in out
    # D-11: the name nothing reads must not come back.
    assert "PLATFORM_MCP_TOKEN" not in out
    # Both accounts are named with their role, so an operator pasting them
    # can tell which is which without reading this script.
    assert _SA_NAME in out
    assert seed._CHAOS_SA_NAME in out
    assert out.count("sa_agent123") == 1, "a token is printed once, in one place"
    assert out.count("sa_chaos456") == 1
