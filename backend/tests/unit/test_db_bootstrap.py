"""Unit tests for the boot-time incident_app password sync (WO-P2-03, F1-01).

Two load-bearing properties, provable without Postgres:

1. No-op discipline: phase-1 boots (INCIDENT_APP_DB_PASSWORD does not
   exist yet) and non-Postgres stacks (SQLite tests, missing URL) must
   not even attempt a connection — the sync runs in scripts/entrypoint.sh
   under `set -e`, so an eager connect would brick every boot that
   predates the Terraform secret.
2. Injection safety: the password travels to the server only as a bound
   parameter into set_config(); the ALTER ROLE DDL (which cannot take
   bind parameters) reads it back server-side with
   format('%L', current_setting(...)). The password must never be
   interpolated into a statement string.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.core import db_bootstrap
from app.core.db_bootstrap import sync_incident_app_password

_PG_URL = "postgresql+asyncpg://owner:ownerpw@db:5432/incident_platform"


def _fake_engine() -> tuple[MagicMock, AsyncMock]:
    """Async-engine stub: `engine.begin()` yields a recording connection."""
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=None)
    engine = MagicMock()
    engine.begin = MagicMock(return_value=ctx)
    engine.dispose = AsyncMock()
    return engine, conn


async def test_noop_when_password_unset() -> None:
    """Phase-1 boots have no INCIDENT_APP_DB_PASSWORD — no connection."""
    with patch.object(db_bootstrap, "create_async_engine") as factory:
        synced = await sync_incident_app_password(_PG_URL, None)
    assert synced is False
    factory.assert_not_called()


async def test_noop_when_password_empty() -> None:
    """An empty env var is 'unset', not a password to sync."""
    with patch.object(db_bootstrap, "create_async_engine") as factory:
        synced = await sync_incident_app_password(_PG_URL, "")
    assert synced is False
    factory.assert_not_called()


async def test_noop_when_url_not_postgres() -> None:
    """SQLite (tests, dev shells) has no roles — no connection."""
    with patch.object(db_bootstrap, "create_async_engine") as factory:
        synced = await sync_incident_app_password("sqlite+aiosqlite://", "pw")
    assert synced is False
    factory.assert_not_called()


async def test_noop_when_no_url_configured() -> None:
    with patch.object(db_bootstrap, "create_async_engine") as factory:
        synced = await sync_incident_app_password(None, "pw")
    assert synced is False
    factory.assert_not_called()


async def test_sync_binds_password_and_never_interpolates_it() -> None:
    """The injection-safety contract of the two-step pattern.

    Statement 1 carries the password strictly as a bound parameter into
    set_config(); statement 2 is a DO block that quote-literals it
    server-side via format('%L', current_setting(...)). A hostile
    password must appear in neither statement string.
    """
    engine, conn = _fake_engine()
    hostile = "p'; DROP ROLE postgres; --"
    with patch.object(db_bootstrap, "create_async_engine", return_value=engine) as factory:
        synced = await sync_incident_app_password(_PG_URL, hostile)

    assert synced is True
    factory.assert_called_once()
    assert conn.execute.await_count == 2

    guc_call = conn.execute.await_args_list[0]
    guc_sql = str(guc_call.args[0])
    assert "set_config('app.role_pw', :pwd, true)" in guc_sql
    assert guc_call.args[1] == {"pwd": hostile}
    assert hostile not in guc_sql

    alter_call = conn.execute.await_args_list[1]
    alter_sql = str(alter_call.args[0])
    assert "ALTER ROLE incident_app LOGIN PASSWORD %L" in alter_sql
    assert "current_setting('app.role_pw')" in alter_sql
    assert hostile not in alter_sql

    engine.dispose.assert_awaited_once()


async def test_both_statements_share_one_transaction() -> None:
    """set_config(..., true) is transaction-local — the ALTER must run in
    the same transaction (engine.begin) or it reads an unset GUC."""
    engine, conn = _fake_engine()
    with patch.object(db_bootstrap, "create_async_engine", return_value=engine):
        await sync_incident_app_password(_PG_URL, "pw")
    engine.begin.assert_called_once()
    engine.connect.assert_not_called()


def test_bootstrap_url_prefers_alembic_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the phase-2 flip DATABASE_URL is the non-owner incident_app;
    the sync (like alembic) must keep using the owner URL when present."""
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", "postgresql+asyncpg://owner@db/x")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://incident_app@db/x")
    assert db_bootstrap._bootstrap_url() == "postgresql+asyncpg://owner@db/x"


def test_bootstrap_url_falls_back_to_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase-1 deploys and the compose migrate one-shot only have DATABASE_URL."""
    monkeypatch.delenv("ALEMBIC_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://owner@db/x")
    assert db_bootstrap._bootstrap_url() == "postgresql+asyncpg://owner@db/x"
