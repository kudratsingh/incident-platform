"""`saturate_db_pool` — hold connections out of the worker process's database pool.

Writes `chaos:db_pool:hold`, which the worker's chaos-only holder task reads once a second
(`app/workers/db_pool_hold.py`, ADR 0031); teardown is the TTL plus the reset's `chaos:*` sweep.
Callers wait to *acquire* a connection while each query, once it has one, runs at its normal
speed — the mirror image of a slow-query fault.
"""

from app.core.logging import get_logger
from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext
from app.workers.db_pool_hold import (
    MAX_HELD_CONNECTIONS,
    MIN_FREE_CONNECTIONS,
    POLL_INTERVAL_SECONDS,
    hold_key,
)
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)

__all__ = ["hold_key", "saturate_db_pool"]


class SaturateDbPoolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connections: int = Field(
        default=MAX_HELD_CONNECTIONS,
        ge=1,
        le=MAX_HELD_CONNECTIONS,
        description=(
            "How many connections to hold checked out. Capped at "
            f"{MAX_HELD_CONNECTIONS}, and clamped further at runtime so at "
            f"least {MIN_FREE_CONNECTIONS} stay acquirable however the pool "
            "is sized — the background loops have to keep running."
        ),
    )
    ttl_seconds: int = Field(
        default=300,
        ge=1,
        le=3600,
        description=(
            "How long the hold lasts. Every connection is given back on the "
            "first pass after it expires — nothing has to be called. "
            "Default 5 minutes."
        ),
    )


class SaturateDbPoolOutput(BaseModel):
    hold_key: str
    connections: int
    ttl_seconds: int
    poll_interval_seconds: float = Field(
        description=(
            "Seconds between the holder's passes. The hold starts, changes "
            "and ends on a pass, so a ttl_seconds smaller than this expires "
            "before any connection is taken."
        )
    )
    min_free_connections: int = Field(
        description=(
            "Connections the runtime clamp always leaves acquirable, so a "
            "held pool slows the worker's loops rather than stopping them."
        )
    )
    accepted: bool = Field(
        description=(
            "True if the flag was set. This does not confirm the connections "
            "are held — that happens on the holder's next pass, at most "
            "poll_interval_seconds away."
        )
    )


@chaos_tool(
    "saturate_db_pool",
    description=(
        "Hold `connections` connections checked out of the worker process's "
        "database pool for `ttl_seconds` (default 300, max 3600), so callers "
        "wait to ACQUIRE a connection while each query, once it has one, runs "
        "at its normal speed. Every connection is released when the flag "
        "expires, when the environment reset clears it, or when the worker "
        "restarts, so the fault is bounded with nothing to call. At least "
        "`min_free_connections` stay acquirable, so the background loops slow "
        "down rather than stop. A repeat call replaces the hold instead of "
        "adding a second one. This is the API and worker process's pool: the "
        "MCP server is a separate process with its own pool, so a pool "
        "reading taken there does not show this fault. Use `saturate_redis` "
        "for pressure on the cache instead — a different dependency."
    ),
    input_model=SaturateDbPoolInput,
    output_model=SaturateDbPoolOutput,
    blast_radius=BlastRadius.SHARED_DEPENDENCY,
)
async def saturate_db_pool(
    inp: SaturateDbPoolInput, ctx: ToolContext
) -> SaturateDbPoolOutput:
    key = hold_key()
    await ctx.redis.set(key, str(inp.connections), ex=inp.ttl_seconds)
    logger.warning(
        "chaos saturate_db_pool set",
        extra={
            "tenant_id": str(ctx.principal.tenant_id),
            "connections": inp.connections,
            "ttl_seconds": inp.ttl_seconds,
        },
    )
    return SaturateDbPoolOutput(
        hold_key=key,
        connections=inp.connections,
        ttl_seconds=inp.ttl_seconds,
        poll_interval_seconds=POLL_INTERVAL_SECONDS,
        min_free_connections=MIN_FREE_CONNECTIONS,
        accepted=True,
    )
