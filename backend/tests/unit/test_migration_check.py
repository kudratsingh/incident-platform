"""Unit tests for the startup schema-drift check.

Real alembic head vs. mocked DB revision. Keeps the check honest
without needing an actual Postgres.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.core.migration_check import (
    SchemaOutOfDateError,
    _expected_head,
    assert_migrations_current,
)


def _session_factory_returning(revision: str | None):
    """Build a session_factory stub that produces sessions whose
    `execute()` returns the given revision (or None if unmigrated)."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=revision)
    session.execute = AsyncMock(return_value=result)

    async def _cm_enter() -> AsyncMock:
        return session

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=None)

    factory = MagicMock(return_value=ctx)
    return factory


def test_expected_head_reads_a_real_revision() -> None:
    """The alembic scripts directory is on disk — the check would be
    useless if it can't find it."""
    head = _expected_head()
    # 12-char hex mixed case string, e.g. "e3f2c6b91d84".
    assert isinstance(head, str)
    assert len(head) == 12
    assert all(c in "0123456789abcdef" for c in head)


async def test_check_passes_when_db_matches_head() -> None:
    head = _expected_head()
    factory = _session_factory_returning(head)
    # Must not raise
    await assert_migrations_current(factory)


async def test_check_raises_when_db_is_behind() -> None:
    factory = _session_factory_returning("a01d04e830dc")  # initial revision
    with pytest.raises(SchemaOutOfDateError) as exc:
        await assert_migrations_current(factory)
    msg = str(exc.value)
    assert "a01d04e830dc" in msg
    assert "docker compose up migrate" in msg


async def test_check_raises_when_alembic_version_empty() -> None:
    """Fresh unmigrated DB — no `alembic_version` row. Still fatal."""
    factory = _session_factory_returning(None)
    with pytest.raises(SchemaOutOfDateError):
        await assert_migrations_current(factory)


async def test_skip_env_var_bypasses_check() -> None:
    """SKIP_MIGRATION_CHECK=1 must not touch the DB — the factory
    would raise if called."""
    factory = MagicMock(
        side_effect=AssertionError("session factory should not be called")
    )
    with patch.dict(os.environ, {"SKIP_MIGRATION_CHECK": "1"}):
        await assert_migrations_current(factory)


async def test_skip_env_var_true_case_insensitive() -> None:
    factory = MagicMock(
        side_effect=AssertionError("session factory should not be called")
    )
    with patch.dict(os.environ, {"SKIP_MIGRATION_CHECK": "TRUE"}):
        await assert_migrations_current(factory)
