"""Database-URL introspection helpers.

Lives in app.core (not alembic/env.py) so tests can import it: env.py
executes the migration runner at import time and must never be imported.
"""

from sqlalchemy.engine import make_url


def is_async_url(url: str) -> bool:
    """True when ``url`` resolves to an async dialect; the driver need not be installed."""
    dialect = make_url(url).get_dialect()
    return bool(getattr(dialect, "is_async", False))
