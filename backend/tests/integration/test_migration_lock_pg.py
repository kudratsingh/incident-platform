"""End-to-end proof that alembic serializes on a real Postgres advisory lock.

The unit tests pin the helper's contract; this module proves the thing the
work order actually cares about (F2-04) against a live server: a second
`alembic upgrade head` cannot run while another holds the lock, which is
what stops two ECS backend tasks from racing on `pg_type` at scale-out.

Two assertions:

  1. The lock is real and mutually exclusive — a second session's
     `pg_advisory_lock` on the same key blocks until the first releases.
  2. env.py actually takes it — with the lock held from this test's own
     session, a subprocess `alembic upgrade head` (the exact command
     scripts/entrypoint.sh runs) makes no progress; released, it completes
     and the schema lands at head. This is the assertion that fails at
     HEAD, where env.py takes no lock and the subprocess sails straight
     through while the lock is held.

The subprocess goes through the async path (postgresql+asyncpg URL, which
is what the container and production both use); env.py routes both the
async and sync paths through the same do_run_migrations, so one lock call
covers both.

Skipped automatically when Docker / testcontainers isn't available so the
rest of the suite still runs.
"""

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from app.core.migration_lock import MIGRATION_LOCK_KEY

REPO_ROOT = Path(__file__).resolve().parents[3]

try:
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    _HAS_TC = True
except Exception:  # pragma: no cover
    _HAS_TC = False

pytestmark = pytest.mark.skipif(
    not _HAS_TC or not os.environ.get("RUN_MIGRATION_LOCK_TEST"),
    reason="set RUN_MIGRATION_LOCK_TEST=1 and install Docker + testcontainers[postgres] to run",
)

# How long we let the blocked party sit before calling it "blocked". Long
# enough that a non-blocking implementation would certainly have finished,
# short enough not to drag the suite.
BLOCK_PROBE_SECONDS = 5.0


@pytest.fixture(scope="module")
def pg() -> Any:
    # driver="asyncpg" so get_connection_url() emits postgresql+asyncpg://
    # (asyncpg is a main dependency; psycopg2 is not installed).
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


@pytest.fixture(scope="module")
def dsn(pg: Any) -> str:
    host = pg.get_container_host_ip()
    port = pg.get_exposed_port(5432)
    return f"postgresql://{pg.username}:{pg.password}@{host}:{port}/{pg.dbname}"


async def test_second_session_blocks_until_the_first_releases(dsn: str) -> None:
    """Session-level pg_advisory_lock on the documented key is exclusive."""
    import asyncio

    import asyncpg

    holder = await asyncpg.connect(dsn)
    waiter = await asyncpg.connect(dsn)
    try:
        await holder.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_KEY)

        pending = asyncio.create_task(
            waiter.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_KEY)
        )
        done, _ = await asyncio.wait({pending}, timeout=2.0)
        assert not done, "second session acquired the lock while the first held it"

        # The lock survives a commit on the holder's session — this is what
        # makes it usable across alembic's internal transaction boundaries
        # (an xact-scoped lock would have been dropped right here).
        async with holder.transaction():
            await holder.execute("SELECT 1")
        done, _ = await asyncio.wait({pending}, timeout=2.0)
        assert not done, "lock was released by an unrelated transaction commit"

        await holder.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_KEY)
        await asyncio.wait_for(pending, timeout=10.0)

        await waiter.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_KEY)
    finally:
        await holder.close()
        await waiter.close()


async def test_alembic_upgrade_blocks_while_the_lock_is_held(pg: Any, dsn: str) -> None:
    """`alembic upgrade head` waits on the lock, then completes and commits.

    Fails at HEAD: without the lock in env.py the subprocess runs to
    completion while this test still holds the lock.
    """
    import asyncpg

    async_url = pg.get_connection_url()
    env = os.environ.copy()
    env["DATABASE_URL"] = async_url
    env.pop("ALEMBIC_DATABASE_URL", None)

    holder = await asyncpg.connect(dsn)
    try:
        await holder.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_KEY)

        proc = subprocess.Popen(
            [sys.executable, "-m", "alembic", "-c", str(REPO_ROOT / "alembic.ini"),
             "upgrade", "head"],
            env=env,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            time.sleep(BLOCK_PROBE_SECONDS)
            assert proc.poll() is None, (
                "alembic upgrade head finished while the migration advisory lock "
                "was held by another session — env.py is not taking the lock"
            )

            # It is genuinely parked on the lock, not merely slow.
            waiting = await holder.fetchval(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND objid = $1 AND NOT granted",
                MIGRATION_LOCK_KEY & 0xFFFFFFFF,
            )
            assert waiting >= 1, "no session is waiting on the migration advisory lock"

            # No schema yet: the waiter has not run a single migration.
            assert not await holder.fetchval("SELECT to_regclass('alembic_version') IS NOT NULL")

            await holder.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_KEY)
            out = proc.communicate(timeout=180)[0]
            assert proc.returncode == 0, out
        finally:
            if proc.poll() is None:  # pragma: no cover - only on assertion failure
                proc.kill()
                proc.communicate(timeout=30)

        # The migration really committed (the lock must not leave alembic
        # looking at an "external" transaction, which would silently make
        # begin_transaction a no-op and roll everything back on close).
        assert await holder.fetchval("SELECT to_regclass('alembic_version') IS NOT NULL")
        assert await holder.fetchval("SELECT count(*) FROM alembic_version") == 1
        assert await holder.fetchval("SELECT to_regclass('jobs') IS NOT NULL")

        # And it left no lock behind on a pooled connection.
        held = await holder.fetchval(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = $1",
            MIGRATION_LOCK_KEY & 0xFFFFFFFF,
        )
        assert held == 0, "the migration advisory lock outlived the alembic run"
    finally:
        await holder.close()
