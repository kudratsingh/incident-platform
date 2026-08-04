"""
`get_redis_health` / `get_postgres_health` — cheap health signals.

Both **fail open** on error: the tool never raises; instead the
`ok` flag is `false` and `error` carries the exception message. The
agent should treat `ok=false` as strong evidence rather than as an
unavailable tool — a red result *is* the useful signal.

Both `telemetry:read`.
"""

from collections.abc import Mapping
from typing import Any

from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text


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


@tool(
    "get_postgres_health",
    description=(
        "Ping the primary database and read a small set of activity "
        "stats. Falls back cleanly on non-Postgres backends (SQLite in "
        "tests) — the `dialect` field tells the agent what it looked at.\n"
        "FRESHNESS: live probe, measured at call time."
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
    try:
        if ctx.db.bind is not None:
            dialect = ctx.db.bind.dialect.name

        t0 = time.perf_counter()
        await ctx.db.execute(text("SELECT 1"))
        latency = (time.perf_counter() - t0) * 1000

        active_conns: int | None = None
        if dialect == "postgresql":
            row = await ctx.db.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE state = 'active'"
                )
            )
            active_conns = int(row.scalar_one())

        return PostgresHealthOutput(
            ok=True,
            ping_latency_ms=round(latency, 3),
            active_connections=active_conns,
            dialect=dialect,
        )
    except Exception as exc:
        return PostgresHealthOutput(ok=False, dialect=dialect, error=str(exc))


def _maybe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _maybe_str(v: Any) -> str | None:
    return str(v) if v is not None else None
