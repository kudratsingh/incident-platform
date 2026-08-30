"""One way to swallow a database error without wrecking the request.

A handler that catches a DB exception and returns a degraded result has
to answer a question the `try/except` does not: what state is the session
in afterwards? On Postgres the answer is "aborted" — every subsequent
statement in that transaction fails with `current transaction is aborted,
commands ignored until end of transaction block`, including the audit
write the MCP envelope does after the tool returns. So on exactly the
failure the fallback exists for, the fallback worked and the audit row
was silently dropped (R2-59).

SQLite does not behave that way, which is why this class of bug survives
a green unit suite: the emulation in
`tests/unit/test_db_degrade.py::AbortingSession` is what makes it
reproducible off Postgres.

The fix is a SAVEPOINT taken before the risky statement. Rolling back to
it discards the failed statement and leaves the enclosing transaction
usable, so the degraded path can still write its own rows. Use this
helper rather than hand-rolling it, so the two behaviours cannot drift:

    probe = ...
    async with degrade_on_db_error(session, what="deploy_markers") as probe:
        rows, total = await repo.list_recent(limit=10)
    if probe.failed:
        ...  # `rows` still holds whatever it was initialised to

Note the assignment inside the block never completes when the query
raises, so the names you bind there keep their pre-block values — give
them defaults before you enter.
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
        """The exception class name — safe to put in a tool's output.

        The message is not: it can carry table names, SQL fragments and
        connection strings, and these outputs go to a machine principal.
        """
        return type(self.error).__name__ if self.error is not None else None


@asynccontextmanager
async def degrade_on_db_error(
    session: AsyncSession,
    *,
    what: str,
    catch: type[BaseException] | tuple[type[BaseException], ...] = SQLAlchemyError,
) -> AsyncIterator[DegradedProbe]:
    """Run a risky query inside a SAVEPOINT; degrade instead of raising.

    On failure the savepoint is rolled back — so the enclosing
    transaction survives and later writes in this request still land —
    and the exception is reported on the yielded probe rather than
    propagated. Anything outside `catch` propagates untouched: a
    degradable read is a specific claim about a specific failure, not a
    licence to swallow every bug in the block.
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
