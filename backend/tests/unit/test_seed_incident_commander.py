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
    its write/chaos grants."""
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
# D-11 — the banner names the env var the commander actually reads
# ---------------------------------------------------------------------------


def test_banner_prints_platform_token(capsys: pytest.CaptureFixture[str]) -> None:
    seed = _seed_module()

    seed._print_banner(_SA_NAME, list(_DEFAULT_SCOPES), False, "sa_secret123")

    out = capsys.readouterr().out
    assert "PLATFORM_TOKEN=sa_secret123" in out
    assert "PLATFORM_MCP_TOKEN" not in out
