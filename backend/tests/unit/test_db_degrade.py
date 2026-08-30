"""R2-59 — swallowing a DB error must leave the session usable.

`degrade_on_db_error` is the one place that answers "and then what?" for
a handler that turns a database failure into a degraded result. These
tests are written against `AbortingSession` (see `tests/conftest.py`),
which reproduces Postgres' aborted-transaction rule on SQLite — without
it the whole failure mode is invisible off Postgres, which is how it
shipped.
"""

import pytest
from app.core.db_degrade import degrade_on_db_error
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import AbortingSession


async def test_the_double_reproduces_the_postgres_rule(
    db_session: AsyncSession,
) -> None:
    """Precondition for everything below: without a savepoint, one failed
    statement really does poison every later one."""
    session = AbortingSession(db_session, fail_on="deploy_markers")

    with pytest.raises(ProgrammingError):
        await session.execute(text("SELECT * FROM deploy_markers"))

    assert session.aborted
    with pytest.raises(ProgrammingError, match="aborted"):
        await session.execute(text("SELECT 1"))


async def test_degraded_query_leaves_the_session_usable(
    db_session: AsyncSession,
) -> None:
    session = AbortingSession(db_session, fail_on="deploy_markers")

    async with degrade_on_db_error(session, what="deploy_markers") as probe:
        await session.execute(text("SELECT * FROM deploy_markers"))

    assert probe.failed
    assert probe.error_type == "ProgrammingError"
    assert not session.aborted, "savepoint rollback should have cleared the abort"
    # The whole point: work that comes after the degraded read still lands.
    assert (await session.execute(text("SELECT 1"))).scalar_one() == 1


async def test_a_successful_query_reports_no_failure(
    db_session: AsyncSession,
) -> None:
    result = None
    async with degrade_on_db_error(db_session, what="ping") as probe:
        result = (await db_session.execute(text("SELECT 1"))).scalar_one()

    assert not probe.failed
    assert probe.error is None
    assert probe.error_type is None
    assert result == 1


async def test_names_bound_inside_the_block_keep_their_defaults_on_failure(
    db_session: AsyncSession,
) -> None:
    """Documented contract — the assignment never completes, so callers
    must initialise before entering."""
    session = AbortingSession(db_session, fail_on="deploy_markers")
    rows: list[str] = []

    async with degrade_on_db_error(session, what="deploy_markers") as probe:
        rows = list(await session.execute(text("SELECT * FROM deploy_markers")))

    assert probe.failed
    assert rows == []


async def test_exceptions_outside_catch_are_not_swallowed(
    db_session: AsyncSession,
) -> None:
    """A degradable read is a claim about a specific failure, not a
    blanket `except Exception` in disguise."""
    with pytest.raises(ValueError, match="not a db problem"):
        async with degrade_on_db_error(db_session, what="ping", catch=SQLAlchemyError):
            raise ValueError("not a db problem")


async def test_catch_can_be_widened_by_the_caller(
    db_session: AsyncSession,
) -> None:
    """`get_postgres_health` widens it to `Exception` on purpose — for a
    health probe, an unclassified driver error is the answer."""
    async with degrade_on_db_error(
        db_session, what="ping", catch=Exception
    ) as probe:
        raise ValueError("driver did something odd")

    assert probe.failed
    assert probe.error_type == "ValueError"
