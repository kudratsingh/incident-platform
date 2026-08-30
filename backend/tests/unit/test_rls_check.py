"""Unit tests for the boot-time RLS posture probe (WO-P2-03, F1-01,
widened by WO-R2-26).

The raise/log/no-op matrix on mocked probe rows plus a real
SQLite-bound session factory. RLS fails to constrain a connection if it
is a superuser (policies never apply), if the table has row-level
security switched off at all, or if the connection owns the table and
FORCE ROW LEVEL SECURITY is off — the last being exactly the posture
production ran with before migration a7e3d9c41f28.

The middle term is the one WO-R2-26 added, and it is why the matrix
below now varies `enabled` and `has_policy` as well: for the intended
non-owner production role the owner term is always False, so a probe
carrying only that term reported ok whatever the server had.

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
from app.core.rls_check import assert_rls_posture, tenant_scoped_tables
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


class _ProbeRow(NamedTuple):
    table_name: str
    is_owner: bool
    enabled: bool
    forced: bool
    has_policy: bool


def _prod_settings() -> Settings:
    # secret_key: production Settings refuse the insecure default at
    # construction; _env_file=None keeps the test hermetic.
    return Settings(_env_file=None, environment="production", secret_key="x" * 48)


def _dev_settings() -> Settings:
    return Settings(_env_file=None, environment="development")


def _rows(
    *,
    is_owner: bool = False,
    forced: bool = True,
    disabled: str | None = None,
    policy_missing: str | None = None,
    omit: str | None = None,
) -> list[_ProbeRow]:
    """One row per tenant-scoped table, healthy unless a named table is
    singled out — the probe has to catch a single bad table among many,
    which is the whole point of widening it past `jobs`."""
    return [
        _ProbeRow(
            table_name=name,
            is_owner=is_owner,
            enabled=name != disabled,
            forced=forced,
            has_policy=name != policy_missing,
        )
        for name in sorted(tenant_scoped_tables())
        if name != omit
    ]


def _pg_session_factory(su: bool, rows: list[_ProbeRow]) -> MagicMock:
    """Session-factory stub reporting a postgresql dialect. The probe
    issues the superuser scalar first, then the per-table query."""
    session = AsyncMock()
    bind = MagicMock()
    bind.dialect.name = "postgresql"
    session.get_bind = MagicMock(return_value=bind)

    su_result = MagicMock()
    su_result.scalar = MagicMock(return_value=su)
    table_result = MagicMock()
    table_result.all = MagicMock(return_value=rows)
    session.execute = AsyncMock(side_effect=[su_result, table_result])

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=ctx)


async def test_superuser_raises_in_production() -> None:
    """Superusers bypass RLS entirely — FORCE doesn't bind them."""
    factory = _pg_session_factory(su=True, rows=_rows())
    with pytest.raises(RuntimeError) as exc:
        await assert_rls_posture(factory, _prod_settings())
    assert "row-level security" in str(exc.value).lower()


async def test_owner_without_force_raises_in_production() -> None:
    """The pre-a7e3d9c41f28 production posture — owner exemption, RLS inert."""
    factory = _pg_session_factory(
        su=False, rows=_rows(is_owner=True, forced=False)
    )
    with pytest.raises(RuntimeError):
        await assert_rls_posture(factory, _prod_settings())


async def test_owner_with_force_passes_in_production() -> None:
    """Phase-1 posture: the owner connection is bound once FORCE is on."""
    factory = _pg_session_factory(su=False, rows=_rows(is_owner=True))
    await assert_rls_posture(factory, _prod_settings())  # no raise


async def test_nonowner_passes_in_production() -> None:
    """Phase-2 posture: the incident_app role — never exempt, FORCE or not."""
    factory = _pg_session_factory(su=False, rows=_rows())
    await assert_rls_posture(factory, _prod_settings())  # no raise


async def test_one_table_with_rls_disabled_raises_in_production() -> None:
    """WO-R2-26, the core of it: a single table with row-level security
    switched off is a hole, and it used to read as 'rls posture ok'.
    Everything else here is healthy and the connection is the intended
    non-owner role, so the old probe had nothing to trip on."""
    factory = _pg_session_factory(su=False, rows=_rows(disabled="job_events"))
    with pytest.raises(RuntimeError) as exc:
        await assert_rls_posture(factory, _prod_settings())
    assert "job_events" in str(exc.value)
    assert "DISABLED" in str(exc.value)


async def test_dropped_tenant_policy_raises_in_production() -> None:
    """ENABLEd and FORCEd with no policy is not the posture the probe
    claims to verify. Postgres denies by default when nothing matches,
    so it does not leak — it silently breaks the table instead."""
    factory = _pg_session_factory(
        su=False, rows=_rows(policy_missing="sagas")
    )
    with pytest.raises(RuntimeError) as exc:
        await assert_rls_posture(factory, _prod_settings())
    assert "sagas" in str(exc.value)
    assert "tenant_isolation" in str(exc.value)


async def test_a_tenant_table_missing_from_the_database_raises() -> None:
    """The probe asks about every tenant-scoped table the ORM declares.
    One that answers nothing is not evidence of a healthy posture."""
    factory = _pg_session_factory(su=False, rows=_rows(omit="alerts"))
    with pytest.raises(RuntimeError) as exc:
        await assert_rls_posture(factory, _prod_settings())
    assert "alerts" in str(exc.value)


async def test_superuser_only_logs_in_development() -> None:
    """Local compose connects as the postgres superuser and MUST keep
    booting; the probe logs ERROR and continues outside production."""
    factory = _pg_session_factory(su=True, rows=_rows(is_owner=True))
    with patch.object(rls_check.logger, "error") as err:
        await assert_rls_posture(factory, _dev_settings())  # no raise
    err.assert_called_once()


async def test_owner_without_force_only_logs_in_development() -> None:
    factory = _pg_session_factory(
        su=False, rows=_rows(is_owner=True, forced=False)
    )
    with patch.object(rls_check.logger, "error") as err:
        await assert_rls_posture(factory, _dev_settings())  # no raise
    err.assert_called_once()


async def test_disabled_rls_only_logs_in_development() -> None:
    factory = _pg_session_factory(su=False, rows=_rows(disabled="jobs"))
    with patch.object(rls_check.logger, "error") as err:
        await assert_rls_posture(factory, _dev_settings())  # no raise
    err.assert_called_once()


async def test_healthy_posture_does_not_log_error() -> None:
    factory = _pg_session_factory(su=False, rows=_rows())
    with patch.object(rls_check.logger, "error") as err:
        await assert_rls_posture(factory, _prod_settings())
    err.assert_not_called()


async def test_noop_on_sqlite_session_factory() -> None:
    """SQLite has no roles and no RLS — the probe must not even run.

    A real aiosqlite engine with no tables: if the probe SQL executed,
    the pg_class reference alone would blow up. Production settings on
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
