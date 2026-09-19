"""What the connection pool holds, and the one number SQLAlchemy does not keep.

A pool reports what it holds right now but nothing about callers that gave up waiting, and
there is no pool event for a checkout timeout — so `CountingQueuePool` records the moment one
is raised and keeps a rolling sixty-second window. Every number here describes the pool of
*this* process; the API and worker processes have their own and are invisible from here, which
`get_postgres_health`'s field descriptions state (ADR 0030).
"""

import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.pool import AsyncAdaptedQueuePool, ConnectionPoolEntry

#: The window `pool_wait_timeouts_1m` counts over. On the wire as part of the field name.
WAIT_TIMEOUT_WINDOW_SECONDS = 60.0

#: Why pool use is unknown. One case: a pool kind that keeps no counters (`StaticPool` under
#: tests, `NullPool` anywhere). Pinned by a test.
POOL_STATS_UNKNOWN_POOL_KIND = (
    "the connection pool behind this reading does not report its counters, so pool use "
    "is unknown"
)

#: Monotonic stamps of checkout timeouts observed in this process, oldest first.
_wait_timeouts: deque[float] = deque()


@dataclass(frozen=True, slots=True)
class PoolStats:
    """One process's pool, as the pool itself reports it."""

    size: int
    checked_out: int
    overflow: int
    max_overflow: int | None
    wait_timeouts_1m: int


def record_pool_wait_timeout(now: float | None = None) -> None:
    """Note that a caller waited for a connection and gave up."""
    _wait_timeouts.append(time.monotonic() if now is None else now)


def pool_wait_timeouts_in_window(now: float | None = None) -> int:
    """How many checkout timeouts this process saw in the last minute.

    A zero here is a measurement, not an unknown: nothing waited. Prunes as it reads, so
    the window cannot grow without bound.
    """
    at = time.monotonic() if now is None else now
    while _wait_timeouts and at - _wait_timeouts[0] > WAIT_TIMEOUT_WINDOW_SECONDS:
        _wait_timeouts.popleft()
    return len(_wait_timeouts)


def reset_pool_wait_timeouts() -> None:
    """Drop the window. Test-only."""
    _wait_timeouts.clear()


class CountingQueuePool(AsyncAdaptedQueuePool):
    """The default async pool, plus a count of the checkouts that timed out.

    There is no pool event for a timeout, so this overrides the one method that raises it.
    `tests/unit/test_health_tools.py` fails if a SQLAlchemy upgrade moves it.
    """

    def _do_get(self) -> ConnectionPoolEntry:
        try:
            return super()._do_get()
        except PoolTimeoutError as err:
            # `_do_get` recurses, so an inner raise would otherwise be counted twice.
            if not getattr(err, "_wait_timeout_counted", False):
                setattr(err, "_wait_timeout_counted", True)  # noqa: B010
                record_pool_wait_timeout()
            raise


def read_pool_stats(pool: Any) -> tuple[PoolStats | None, str | None]:
    """`(this process's pool use, reason it is unknown)` — exactly one is populated."""
    size = getattr(pool, "size", None)
    checked_out = getattr(pool, "checkedout", None)
    overflow = getattr(pool, "overflow", None)
    if not callable(size) or not callable(checked_out) or not callable(overflow):
        return None, POOL_STATS_UNKNOWN_POOL_KIND

    try:
        stats = PoolStats(
            size=int(size()),
            checked_out=int(checked_out()),
            overflow=int(overflow()),
            max_overflow=_max_overflow(pool),
            wait_timeouts_1m=pool_wait_timeouts_in_window(),
        )
    except Exception:
        return None, POOL_STATS_UNKNOWN_POOL_KIND
    return stats, None


def _max_overflow(pool: Any) -> int | None:
    """The ceiling above `size`, which QueuePool keeps privately; `None` when unstated."""
    value = getattr(pool, "_max_overflow", None)
    return int(value) if isinstance(value, int) else None


__all__ = [
    "POOL_STATS_UNKNOWN_POOL_KIND",
    "WAIT_TIMEOUT_WINDOW_SECONDS",
    "CountingQueuePool",
    "PoolStats",
    "pool_wait_timeouts_in_window",
    "read_pool_stats",
    "record_pool_wait_timeout",
    "reset_pool_wait_timeouts",
]
