"""The documented ways to run migrations, against a real Postgres (WO-R2-67).

Neither worked: `make migrate` ran alembic as the non-owner `incident_app` role, and the
README's `cd backend && alembic upgrade head` cannot find alembic.ini. The first test
counts tables, because a run that reports success and rolls back on close passes the rest.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

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


@pytest.fixture(scope="module")
def pg() -> Any:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


def _owner_dsn(pg: Any, database: str | None = None) -> str:
    host = pg.get_container_host_ip()
    port = pg.get_exposed_port(5432)
    return (
        f"postgresql+asyncpg://{pg.username}:{pg.password}@{host}:{port}/"
        f"{database or pg.dbname}"
    )


def _run_alembic(dsn: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """`alembic upgrade head` exactly as an operator would run it."""
    env = {
        **os.environ,
        "ALEMBIC_DATABASE_URL": dsn,
        "PYTHONPATH": str(REPO_ROOT / "backend"),
    }
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


async def _table_count(dsn: str) -> int:
    import asyncpg  # type: ignore[import-untyped]

    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        return int(
            await conn.fetchval(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
        )
    finally:
        await conn.close()


async def test_owner_migration_from_the_repo_root_creates_the_schema(
    pg: Any,
) -> None:
    """The owner, from the repo root; tables really land."""
    dsn = _owner_dsn(pg)

    result = _run_alembic(dsn, cwd=REPO_ROOT)

    assert result.returncode == 0, result.stderr
    assert await _table_count(dsn) > 10, (
        "alembic reported success but the schema is empty — the migration "
        "transaction was never committed"
    )


async def test_rerunning_is_a_no_op(pg: Any) -> None:
    dsn = _owner_dsn(pg)
    _run_alembic(dsn, cwd=REPO_ROOT)
    before = await _table_count(dsn)

    result = _run_alembic(dsn, cwd=REPO_ROOT)

    assert result.returncode == 0, result.stderr
    assert await _table_count(dsn) == before


async def test_a_non_owner_role_is_refused_with_a_useful_message(
    pg: Any,
) -> None:
    """What `make migrate` did: runtime role, fails mid-CREATE."""
    import asyncpg  # type: ignore[import-untyped]

    owner = _owner_dsn(pg)
    conn = await asyncpg.connect(owner.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        # Create-if-absent: the role may already own dependent objects.
        await conn.execute(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'incident_app') "
            "THEN CREATE ROLE incident_app LOGIN PASSWORD 'localdev'; END IF; "
            "END $$;"
        )
        await conn.execute("ALTER ROLE incident_app WITH LOGIN PASSWORD 'localdev'")
        await conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        await conn.execute("REVOKE CREATE ON SCHEMA public FROM incident_app")
        await conn.execute("GRANT USAGE ON SCHEMA public TO incident_app")
    finally:
        await conn.close()

    host, port = pg.get_container_host_ip(), pg.get_exposed_port(5432)
    non_owner = (
        f"postgresql+asyncpg://incident_app:localdev@{host}:{port}/{pg.dbname}"
    )

    result = _run_alembic(non_owner, cwd=REPO_ROOT)

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "cannot CREATE in schema public" in combined
    # The message has to name the way out, or it just relocates the puzzle.
    assert "ALEMBIC_DATABASE_URL" in combined


def test_the_readme_command_runs_from_the_repo_root_only() -> None:
    """alembic.ini is at the repo root and the CLI does not search up."""
    assert (REPO_ROOT / "alembic.ini").exists()
    assert not (REPO_ROOT / "backend" / "alembic.ini").exists()

    from_backend = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT / "backend",
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "backend")},
        capture_output=True,
        text=True,
    )

    assert from_backend.returncode != 0
    assert "script_location" in (from_backend.stdout + from_backend.stderr)

    readme = (REPO_ROOT / "README.md").read_text("utf-8")
    assert "cd backend && alembic upgrade head" not in readme
