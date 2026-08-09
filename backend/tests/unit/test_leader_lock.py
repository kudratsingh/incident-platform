"""Unit tests for the advisory-lock leader gate (`app.core.leader_lock`).

These pin the *contract* — one connection for the lock's whole lifetime,
release on the way out, never hand a possibly-locked connection back to
the pool. They cannot prove mutual exclusion: SQLite has no advisory
locks and the unit tier is one process. That proof is
`backend/tests/integration/test_outbox_relay_concurrency.py`.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from app.core.leader_lock import (
    OUTBOX_RELAY_LOCK_KEY,
    advisory_leader_lock,
    resolve_engine,
)
from app.core.migration_lock import MIGRATION_LOCK_KEY
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class _FakeConn:
    """Records every statement so a test can prove which connection ran it."""

    def __init__(self, acquired: bool, release_error: Exception | None = None) -> None:
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0
        self.closed = 0
        self.invalidated = 0
        self._acquired = acquired
        self._release_error = release_error

    async def execute(self, stmt: Any, params: dict[str, Any]) -> Any:
        sql = str(stmt)
        self.executed.append((sql, params))
        if "pg_advisory_unlock" in sql and self._release_error is not None:
            raise self._release_error
        return MagicMock(scalar=MagicMock(return_value=self._acquired))

    async def commit(self) -> None:
        self.commits += 1

    async def close(self) -> None:
        self.closed += 1

    async def invalidate(self) -> None:
        self.invalidated += 1


class _FakeEngine:
    """Postgres-shaped engine that mints a fresh connection per connect().

    Minting a new object each time is the point: if the gate ever checked
    out a second connection to release on, `conns` would have two entries
    and the "same connection" assertion below would catch it.
    """

    def __init__(self, acquired: bool = True, release_error: Exception | None = None) -> None:
        self.dialect = SimpleNamespace(name="postgresql")
        self.conns: list[_FakeConn] = []
        self._acquired = acquired
        self._release_error = release_error

    async def connect(self) -> _FakeConn:
        conn = _FakeConn(self._acquired, self._release_error)
        self.conns.append(conn)
        return conn


def _statements(conn: _FakeConn) -> str:
    return " | ".join(sql for sql, _ in conn.executed)


@pytest.fixture
def pg_gate(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Swap engine resolution for a fake Postgres engine."""

    def _make(**kwargs: Any) -> _FakeEngine:
        engine = _FakeEngine(**kwargs)
        monkeypatch.setattr(
            "app.core.leader_lock.resolve_engine", lambda _factory: engine
        )
        return engine

    return _make


# ---------------------------------------------------------------------------
# The connection-scoping contract — THE trap this module exists to contain
# ---------------------------------------------------------------------------


async def test_acquire_and_release_run_on_one_pinned_connection(pg_gate: Any) -> None:
    """Advisory locks are connection-scoped; a pooled second connection
    would 'release' a lock this process never held and leak the real one."""
    engine = pg_gate()

    async with advisory_leader_lock(MagicMock(), OUTBOX_RELAY_LOCK_KEY) as is_leader:
        assert is_leader is True

    assert len(engine.conns) == 1, "the gate checked out more than one connection"
    conn = engine.conns[0]
    assert "pg_try_advisory_lock" in _statements(conn)
    assert "pg_advisory_unlock" in _statements(conn)
    assert [params["key"] for _, params in conn.executed] == [
        OUTBOX_RELAY_LOCK_KEY,
        OUTBOX_RELAY_LOCK_KEY,
    ]
    assert conn.closed == 1


async def test_autobegun_transaction_is_committed_around_the_lock(
    pg_gate: Any,
) -> None:
    """SQLAlchemy 2.0 autobegins on the first execute. Left open, the
    connection would sit `idle in transaction` for the whole tick."""
    engine = pg_gate()

    async with advisory_leader_lock(MagicMock(), OUTBOX_RELAY_LOCK_KEY):
        assert engine.conns[0].commits == 1  # acquire's tx already ended

    assert engine.conns[0].commits == 2  # release's too


async def test_lock_is_released_even_when_the_body_raises(pg_gate: Any) -> None:
    engine = pg_gate()

    with pytest.raises(RuntimeError):
        async with advisory_leader_lock(MagicMock(), OUTBOX_RELAY_LOCK_KEY):
            raise RuntimeError("tick blew up")

    assert "pg_advisory_unlock" in _statements(engine.conns[0])
    assert engine.conns[0].closed == 1


async def test_loser_yields_false_and_never_unlocks(pg_gate: Any) -> None:
    """`pg_advisory_unlock` for a lock the session never took warns and
    returns false — the loser must not call it."""
    engine = pg_gate(acquired=False)

    async with advisory_leader_lock(MagicMock(), OUTBOX_RELAY_LOCK_KEY) as is_leader:
        assert is_leader is False

    conn = engine.conns[0]
    assert "pg_advisory_unlock" not in _statements(conn)
    assert conn.closed == 1


async def test_failed_release_invalidates_instead_of_pooling_the_connection(
    pg_gate: Any,
) -> None:
    """A still-locked connection back in the pool would wedge every
    replica out of the relay until it happened to be recycled."""
    engine = pg_gate(release_error=RuntimeError("connection reset"))

    with pytest.raises(RuntimeError):
        async with advisory_leader_lock(MagicMock(), OUTBOX_RELAY_LOCK_KEY):
            pass

    assert engine.conns[0].invalidated == 1


# ---------------------------------------------------------------------------
# Engine resolution + the non-Postgres no-op
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sqlite_factory():  # type: ignore[no-untyped-def]
    engine = create_async_engine("sqlite+aiosqlite://")
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_resolve_engine_finds_the_bound_engine(sqlite_factory: Any) -> None:
    """Pins how the bind is read off `async_sessionmaker`. If a SQLAlchemy
    upgrade moves it, this fails loudly rather than silently turning the
    gate into an always-leader no-op in production."""
    engine = resolve_engine(sqlite_factory)
    assert engine is not None
    assert engine.dialect.name == "sqlite"


async def test_resolve_engine_returns_none_without_an_engine() -> None:
    assert resolve_engine(MagicMock()) is None


async def test_gate_is_a_noop_on_sqlite(sqlite_factory: Any) -> None:
    """The unit/API tiers run one process against SQLite — no advisory
    locks to take, and nothing to be excluded from."""
    async with advisory_leader_lock(sqlite_factory, OUTBOX_RELAY_LOCK_KEY) as is_leader:
        assert is_leader is True


async def test_gate_fails_open_when_the_engine_cannot_be_resolved() -> None:
    """Better a duplicate publish than a relay that silently stops."""
    async with advisory_leader_lock(MagicMock(), OUTBOX_RELAY_LOCK_KEY) as is_leader:
        assert is_leader is True


def test_relay_key_does_not_collide_with_the_migration_lock() -> None:
    """Same lock space: a collision would make `alembic upgrade head`
    and the relay exclude each other."""
    assert OUTBOX_RELAY_LOCK_KEY != MIGRATION_LOCK_KEY
