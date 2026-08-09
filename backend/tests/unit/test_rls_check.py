"""Unit tests for the boot-time RLS posture probe (WO-P2-03, F1-01).

The raise/log/no-op matrix on mocked probe rows plus a real
SQLite-bound session factory. RLS is inert for a connection iff it is a
superuser, or it owns the tables and FORCE ROW LEVEL SECURITY is off —
exactly the posture production ran with before migration a7e3d9c41f28
(owner, unforced): this probe would have flagged it on the first deploy.

Hard failure is production-only: local compose historically connected as
the postgres superuser and the commander's eval stack (pinned image +
its own compose) must keep booting — outside production the probe logs
ERROR and continues.
"""

from typing import NamedTuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import Settings
from app.core import rls_check
from app.core.rls_check import assert_rls_posture
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


class _ProbeRow(NamedTuple):
    su: bool
    is_owner: bool
    forced: bool


def _prod_settings() -> Settings:
    # secret_key: production Settings refuse the insecure default at
    # construction; _env_file=None keeps the test hermetic.
    return Settings(_env_file=None, environment="production", secret_key="x" * 48)


def _dev_settings() -> Settings:
    return Settings(_env_file=None, environment="development")


def _pg_session_factory(row: _ProbeRow) -> MagicMock:
    """Session-factory stub whose session reports a postgresql dialect
    and returns `row` from the posture probe."""
    session = AsyncMock()
    bind = MagicMock()
    bind.dialect.name = "postgresql"
    session.get_bind = MagicMock(return_value=bind)
    result = MagicMock()
    result.one = MagicMock(return_value=row)
    session.execute = AsyncMock(return_value=result)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=ctx)


async def test_superuser_raises_in_production() -> None:
    """Superusers bypass RLS entirely — FORCE doesn't bind them."""
    factory = _pg_session_factory(_ProbeRow(su=True, is_owner=False, forced=True))
    with pytest.raises(RuntimeError) as exc:
        await assert_rls_posture(factory, _prod_settings())
    assert "row-level security" in str(exc.value).lower()


async def test_owner_without_force_raises_in_production() -> None:
    """The pre-a7e3d9c41f28 production posture — owner exemption, RLS inert."""
    factory = _pg_session_factory(_ProbeRow(su=False, is_owner=True, forced=False))
    with pytest.raises(RuntimeError):
        await assert_rls_posture(factory, _prod_settings())


async def test_owner_with_force_passes_in_production() -> None:
    """Phase-1 posture: the owner connection is bound once FORCE is on."""
    factory = _pg_session_factory(_ProbeRow(su=False, is_owner=True, forced=True))
    await assert_rls_posture(factory, _prod_settings())  # no raise


async def test_nonowner_passes_in_production() -> None:
    """Phase-2 posture: the incident_app role — never exempt, FORCE or not."""
    factory = _pg_session_factory(_ProbeRow(su=False, is_owner=False, forced=True))
    await assert_rls_posture(factory, _prod_settings())  # no raise


async def test_superuser_only_logs_in_development() -> None:
    """Local compose connects as the postgres superuser and MUST keep
    booting; the probe logs ERROR and continues outside production."""
    factory = _pg_session_factory(_ProbeRow(su=True, is_owner=True, forced=True))
    with patch.object(rls_check.logger, "error") as err:
        await assert_rls_posture(factory, _dev_settings())  # no raise
    err.assert_called_once()


async def test_owner_without_force_only_logs_in_development() -> None:
    factory = _pg_session_factory(_ProbeRow(su=False, is_owner=True, forced=False))
    with patch.object(rls_check.logger, "error") as err:
        await assert_rls_posture(factory, _dev_settings())  # no raise
    err.assert_called_once()


async def test_healthy_posture_does_not_log_error() -> None:
    factory = _pg_session_factory(_ProbeRow(su=False, is_owner=False, forced=True))
    with patch.object(rls_check.logger, "error") as err:
        await assert_rls_posture(factory, _prod_settings())
    err.assert_not_called()


async def test_noop_on_sqlite_session_factory() -> None:
    """SQLite has no roles and no RLS — the probe must not even run.

    A real aiosqlite engine with no tables: if the probe SQL executed,
    the ::regclass cast alone would blow up. Production settings on
    purpose — the dialect check, not the environment, is what gates.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await assert_rls_posture(factory, _prod_settings())  # no raise
    finally:
        await engine.dispose()
