"""Swallow a database error without wrecking the rest of the request.

On Postgres a failed statement aborts the whole transaction, so a bare
`except SQLAlchemyError` around a degradable query also kills every later write
in it — including the MCP envelope's audit row (R2-59). SQLite does not, hence
`tests/unit/test_db_degrade.py::AbortingSession`. Always take a SAVEPOINT: use
this helper rather than hand-rolling it, and give names bound inside the block a
default beforehand, because the assignment never completes on failure.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from app.core.logging import get_logger
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)


@dataclass
class DegradedProbe:
    """What the caller learns about an attempt that may have degraded."""

    error: BaseException | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None

    @property
    def error_type(self) -> str | None:
        """The exception class name. The message is not safe: it leaks tables and SQL."""
        return type(self.error).__name__ if self.error is not None else None


@asynccontextmanager
async def degrade_on_db_error(
    session: AsyncSession,
    *,
    what: str,
    catch: type[BaseException] | tuple[type[BaseException], ...] = SQLAlchemyError,
) -> AsyncIterator[DegradedProbe]:
    """Run a risky query inside a SAVEPOINT; degrade instead of raising.

    On failure the savepoint rolls back, so later writes still land, and the
    error lands on the probe. Anything outside `catch` propagates untouched.
    """
    probe = DegradedProbe()
    try:
        async with session.begin_nested():
            yield probe
    except catch as exc:  # noqa: B902 — `catch` is the caller's contract
        probe.error = exc
        logger.warning(
            "db query failed; rolled back to savepoint and degrading",
            extra={
                "what": what,
                "error_type": type(exc).__name__,
                "error": str(exc)[:200],
            },
        )


__all__ = ["DegradedProbe", "degrade_on_db_error"]
