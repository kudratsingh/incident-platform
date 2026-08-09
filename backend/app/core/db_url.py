"""Database-URL introspection helpers.

Lives in app.core (not alembic/env.py) so tests can import it: env.py
executes the migration runner at import time and must never be imported.
"""

from sqlalchemy.engine import make_url


def is_async_url(url: str) -> bool:
    """Return True if ``url`` resolves to an asyncio SQLAlchemy dialect.

    Resolves the dialect class only — the underlying DBAPI module is not
    imported, so this works even when the driver (e.g. psycopg2) is not
    installed.
    """
    dialect = make_url(url).get_dialect()
    return bool(getattr(dialect, "is_async", False))
