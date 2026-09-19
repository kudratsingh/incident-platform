"""
`get_redis_health` / `get_postgres_health` — cheap health signals.

Both fail open: never raise, set `ok=false`, message in `error`. The Postgres probe
sits in a SAVEPOINT, so `ok=false` leaves the caller's transaction writable (R2-59).
Both `telemetry:read`. The pool and query readings (WO-R3-217) are what lets a caller
tell slow queries from a saturated pool; what neither can be measured from is said
rather than guessed at (ADR 0030).
"""

from collections.abc import Mapping
from typing import Any

from app.core.db_degrade import degrade_on_db_error
from app.core.db_pool_stats import PoolStats, read_pool_stats
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

#: What counts as a slow query, in milliseconds. A fixed platform threshold rather than a
#: caller argument, and reported beside the count so the number can be read at all.
SLOW_QUERY_THRESHOLD_MS = 500.0

#: Why per-query timing history is unknown. Closed set, pinned by a test — a caller may key
#: on these strings.
QUERY_STATS_UNKNOWN_NOT_POSTGRES = (
    "this database is not PostgreSQL, so it collects no per-query timing statistics"
)
QUERY_STATS_UNKNOWN_EXTENSION_ABSENT = (
    "the PostgreSQL extension that records per-query timings is not installed on this "
    "database, so no per-query timing history exists"
)
QUERY_STATS_UNKNOWN_NO_WINDOWED_HISTORY = (
    "the platform keeps no per-minute history of query timings: the statistics it can "
    "read are totals since they were last reset, which cannot be narrowed to one minute"
)
QUERY_STATS_UNKNOWN_REASONS = (
    QUERY_STATS_UNKNOWN_NOT_POSTGRES,
    QUERY_STATS_UNKNOWN_EXTENSION_ABSENT,
    QUERY_STATS_UNKNOWN_NO_WINDOWED_HISTORY,
)

_EXTENSION_PRESENT = text(
    "SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'"
)

# Own backend excluded: this statement is itself an active query, and counting it would
# report the probe as the platform's slowest work. `clock_timestamp()` rather than `now()`
# because `now()` is the transaction's start time, which would age with the transaction.
_ACTIVE_QUERY_AGES = text(
    """
    SELECT
      max(extract(epoch FROM (clock_timestamp() - query_start)) * 1000) AS longest_ms,
      count(*) FILTER (
        WHERE extract(epoch FROM (clock_timestamp() - query_start)) * 1000
              > :threshold_ms
      ) AS over_threshold
    FROM pg_stat_activity
    WHERE state = 'active'
      AND query_start IS NOT NULL
      AND datname = current_database()
      AND pid <> pg_backend_pid()
    """
)


class _EmptyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RedisHealthOutput(BaseModel):
    ok: bool
    ping_latency_ms: float | None = Field(
        default=None,
        description="Round-trip time for a single PING. `null` when the "
        "ping failed.",
    )
    connected_clients: int | None = None
    used_memory_bytes: int | None = None
    used_memory_human: str | None = None
    keyspace_hits: int | None = None
    keyspace_misses: int | None = None
    error: str | None = None


@tool(
    "get_redis_health",
    description=(
        "Ping Redis and read a short list of stats from INFO. Never "
        "raises — an `ok=false` result *is* the interesting signal.\n"
        "FRESHNESS: live probe, measured at call time. Reports "
        "aggregate server stats only — it does not enumerate keys, so "
        "it cannot tell you which keys exist or who wrote them."
    ),
    input_model=_EmptyIn,
    output_model=RedisHealthOutput,
    required_scope=Scope.TELEMETRY_READ,
)
async def get_redis_health(
    _inp: _EmptyIn, ctx: ToolContext
) -> RedisHealthOutput:
    import time

    try:
        t0 = time.perf_counter()
        pong = await ctx.redis.ping()
        latency = (time.perf_counter() - t0) * 1000
        if not pong:
            return RedisHealthOutput(ok=False, error="ping returned falsy")
        info: Mapping[str, Any] = await ctx.redis.info()
        return RedisHealthOutput(
            ok=True,
            ping_latency_ms=round(latency, 3),
            connected_clients=_maybe_int(info.get("connected_clients")),
            used_memory_bytes=_maybe_int(info.get("used_memory")),
            used_memory_human=_maybe_str(info.get("used_memory_human")),
            keyspace_hits=_maybe_int(info.get("keyspace_hits")),
            keyspace_misses=_maybe_int(info.get("keyspace_misses")),
        )
    except Exception as exc:
        return RedisHealthOutput(ok=False, error=str(exc))


class PostgresHealthOutput(BaseModel):
    ok: bool
    ping_latency_ms: float | None = None
    active_connections: int | None = Field(
        default=None,
        description="Rows in pg_stat_activity where state='active'. "
        "Null on SQLite (dev/test) — that path returns a bare `ok=true`.",
    )
    dialect: str
    error: str | None = None

    pool_size: int | None = Field(
        default=None,
        description="How many connections the pool of the process that answered this "
        "call keeps open before it opens extras. `null` when that pool keeps no "
        "counters — see `pool_stats_unknown_reason`.",
    )
    pool_checked_out: int | None = Field(
        default=None,
        description="Connections in use right now, in the pool of the process that "
        "answered this call — **including the one this call is holding**, so on an "
        "otherwise idle process it reads 1 rather than 0. Read it against `pool_size` "
        "and `pool_max_overflow`: on its own a number here says nothing. `null` means "
        "unknown, never idle.",
    )
    pool_overflow: int | None = Field(
        default=None,
        description="Connections currently open beyond `pool_size`, in the pool of the "
        "process that answered this call. 0 until the pool is full, then climbing to "
        "`pool_max_overflow`, at which point the next caller waits. `null` means "
        "unknown.",
    )
    pool_max_overflow: int | None = Field(
        default=None,
        description="How many connections beyond `pool_size` that pool may open. The "
        "ceiling `pool_overflow` is read against. `null` when the pool does not state "
        "one.",
    )
    pool_wait_timeouts_1m: int | None = Field(
        default=None,
        description="How many callers in the process that answered this call waited for "
        "a connection and gave up in the last 60 seconds. A 0 here is a real "
        "measurement — nothing waited — while `null` is unknown. Counted per process, "
        "so it says nothing about the API or worker processes.",
    )
    pool_stats_unknown_reason: str | None = Field(
        default=None,
        description="Why every `pool_*` field above is null, in plain words. `null` "
        "exactly when they are populated.",
    )

    longest_active_query_ms: float | None = Field(
        default=None,
        description="How long the longest query running right now has been running, in "
        "milliseconds, on the database server's clock. Covers every connection to this "
        "database from every process, not just this one, and excludes this reading's own "
        "query. `null` when nothing is running, or when the database keeps no such view "
        "— see `query_stats_unknown_reason`.",
    )
    active_queries_over_slow_threshold: int | None = Field(
        default=None,
        description="How many queries running right now have been running longer than "
        "`slow_query_threshold_ms`. A live count across every connection to this "
        "database; 0 means nothing is currently slow, `null` means unknown.",
    )
    slow_query_threshold_ms: float = Field(
        description="The threshold the count above is measured against, in "
        "milliseconds. Fixed by the platform; there is no argument to change it."
    )
    p95_query_ms_1m: float | None = Field(
        default=None,
        description="Always `null` in this release: the platform cannot measure a "
        "query-latency percentile over the last minute, and `query_stats_unknown_reason` "
        "says why. Use `longest_active_query_ms` and "
        "`active_queries_over_slow_threshold` for query slowness instead.",
    )
    slow_query_count_1m: int | None = Field(
        default=None,
        description="Always `null` in this release, with `query_stats_unknown_reason` "
        "saying why — the platform keeps no per-minute query history. "
        "`active_queries_over_slow_threshold` is the live equivalent.",
    )
    query_stats_unknown_reason: str | None = Field(
        default=None,
        description="Why the two per-minute query fields are null, in plain words: one "
        "of three fixed sentences saying which case it is. `null` only if they are ever "
        "populated.",
    )


@tool(
    "get_postgres_health",
    description=(
        "Ping the primary database and read how it is being used: connection "
        "activity, the connection pool this reading came from, and how long "
        "queries are taking right now. Never raises — an `ok=false` result *is* "
        "the interesting signal, and the `dialect` field says what was looked "
        "at (SQLite in dev and tests skips the Postgres-only readings).\n"
        "FRESHNESS AND WHICH CLOCK. Nothing here is cached. Every number is read "
        "at call time; `ping_latency_ms` is timed in the process that answered, "
        "and `longest_active_query_ms` is measured on the database server's "
        "clock, so the two are not comparable to the millisecond.\n"
        "NO PAGING, NOTHING CAPPED. This tool returns counts and timings, never "
        "rows. It takes no arguments, there is no `limit` and no `offset`, and "
        "nothing is truncated.\n"
        "WHOSE POOL. The `pool_*` fields describe the connection pool of the "
        "process that answered this call, and only that one. This platform runs "
        "the API, the worker and this read surface as separate processes, each "
        "with its own pool, so a pool exhausted in the API or the worker does "
        "not show up here. The query fields have no such limit: they are read "
        "from the database server, so they cover every connection to it.\n"
        "TELLING SLOW QUERIES FROM A FULL POOL. These are different faults with "
        "different fixes, and one reading separates them. Queries slow: "
        "`longest_active_query_ms` and `active_queries_over_slow_threshold` "
        "high, pool counters normal. Pool saturated: `pool_checked_out` at "
        "`pool_size` plus `pool_max_overflow` with `pool_wait_timeouts_1m` "
        "climbing, while queries themselves are not slow.\n"
        "UNKNOWN IS NULL, NEVER 0. A `null` count is missing information; a 0 is "
        "a measurement that found nothing. `pool_stats_unknown_reason` and "
        "`query_stats_unknown_reason` say which case a null is. Two fields, "
        "`p95_query_ms_1m` and `slow_query_count_1m`, are null in every "
        "response: the platform keeps no per-minute history of query timings, "
        "and says so rather than reporting a number it cannot stand behind.\n"
        "WHAT THIS CANNOT SEE. It reads one database's own views and one "
        "process's pool. It says nothing about replicas, nothing about which "
        "statements are slow (no query text is returned), and a healthy reading "
        "here does not mean callers are getting served — a pool full in another "
        "process looks perfectly healthy from this one."
    ),
    input_model=_EmptyIn,
    output_model=PostgresHealthOutput,
    required_scope=Scope.TELEMETRY_READ,
)
async def get_postgres_health(
    _inp: _EmptyIn, ctx: ToolContext
) -> PostgresHealthOutput:
    import time

    dialect = "unknown"
    healthy: PostgresHealthOutput | None = None

    # SAVEPOINT around the probe: `ok=false` is a report, not a licence to leave the
    # session wrecked. Before R2-59 an unhealthy probe aborted the Postgres
    # transaction, taking this call's own audit row with it.
    #
    # `catch=Exception` is deliberate: an unclassified driver error is itself the
    # answer a health check gives.
    async with degrade_on_db_error(
        ctx.db, what="postgres health probe", catch=Exception
    ) as probe:
        if ctx.db.bind is not None:
            dialect = ctx.db.bind.dialect.name

        t0 = time.perf_counter()
        await ctx.db.execute(text("SELECT 1"))
        latency = (time.perf_counter() - t0) * 1000

        active_conns: int | None = None
        longest_ms: float | None = None
        over_threshold: int | None = None
        query_unknown = QUERY_STATS_UNKNOWN_NOT_POSTGRES
        if dialect == "postgresql":
            row = await ctx.db.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE state = 'active'"
                )
            )
            active_conns = int(row.scalar_one())

            ages = (
                await ctx.db.execute(
                    _ACTIVE_QUERY_AGES, {"threshold_ms": SLOW_QUERY_THRESHOLD_MS}
                )
            ).one()
            longest_ms = (
                None if ages.longest_ms is None else round(float(ages.longest_ms), 3)
            )
            over_threshold = int(ages.over_threshold or 0)

            # Present or absent, there is no windowed history either way — the two
            # reasons differ because "not installed" and "installed and still cannot
            # answer this" are different facts about the platform (ADR 0030).
            installed = (await ctx.db.execute(_EXTENSION_PRESENT)).first() is not None
            query_unknown = (
                QUERY_STATS_UNKNOWN_NO_WINDOWED_HISTORY
                if installed
                else QUERY_STATS_UNKNOWN_EXTENSION_ABSENT
            )

        healthy = PostgresHealthOutput(
            ok=True,
            ping_latency_ms=round(latency, 3),
            active_connections=active_conns,
            dialect=dialect,
            longest_active_query_ms=longest_ms,
            active_queries_over_slow_threshold=over_threshold,
            slow_query_threshold_ms=SLOW_QUERY_THRESHOLD_MS,
            query_stats_unknown_reason=query_unknown,
        )

    # Read the pool AFTER the probe, never before it: a session checks a connection out
    # lazily, on its first statement, so a reading taken at the top of this handler counts
    # the pool as empty and misses this call's own connection (CI caught exactly that —
    # `pool_checked_out: 0` on a live Postgres). It stays outside the SAVEPOINT because it
    # is in-process state: a database refusing statements must not make pool use unknown.
    pool_stats, pool_unknown = read_pool_stats(
        getattr(ctx.db.bind, "pool", None) if ctx.db.bind is not None else None
    )
    if healthy is not None and not probe.failed:
        healthy = healthy.model_copy(update=_pool_fields(pool_stats, pool_unknown))

    if probe.failed or healthy is None:
        return PostgresHealthOutput(
            ok=False,
            dialect=dialect,
            error=str(probe.error),
            slow_query_threshold_ms=SLOW_QUERY_THRESHOLD_MS,
            # A failed probe leaves every query reading unknown, and says which unknown.
            query_stats_unknown_reason=(
                QUERY_STATS_UNKNOWN_NOT_POSTGRES
                if dialect != "postgresql"
                else QUERY_STATS_UNKNOWN_NO_WINDOWED_HISTORY
            ),
            **_pool_fields(pool_stats, pool_unknown),
        )
    return healthy


def _pool_fields(
    stats: PoolStats | None, unknown_reason: str | None
) -> dict[str, Any]:
    """The five pool fields, or five nulls and the reason they are null."""
    if stats is None:
        return {"pool_stats_unknown_reason": unknown_reason}
    return {
        "pool_size": stats.size,
        "pool_checked_out": stats.checked_out,
        "pool_overflow": stats.overflow,
        "pool_max_overflow": stats.max_overflow,
        "pool_wait_timeouts_1m": stats.wait_timeouts_1m,
    }


def _maybe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _maybe_str(v: Any) -> str | None:
    return str(v) if v is not None else None
