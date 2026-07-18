"""
`saturate_redis` — write N synthetic keys of size M bytes to Redis.

Real effect: memory pressure. Depending on the target Redis's
`maxmemory-policy` this may trigger key eviction — meaning the
platform's rate-limit counters, backpressure cache, and CQRS
read-model sets can start disappearing. The agent's job is to notice
the resulting anomalies, not to prevent them.

Every key is written with a short TTL (`ttl_seconds`, default 60) so
the effect self-cleans without operator action.

Keys are namespaced under `chaos:sat:{run_id}:{i}` — the `run_id`
lets the agent inspect + optionally clear its own footprint without
touching other traffic.

Requires `chaos:invoke`. Registered only when `CHAOS_ENABLED=true`.
"""

import uuid

from app.core.logging import get_logger
from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)


class SaturateRedisInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    num_keys: int = Field(
        default=1000,
        ge=1,
        le=100_000,
        description="How many keys to write.",
    )
    value_bytes: int = Field(
        default=1024,
        ge=16,
        le=1_048_576,
        description="Byte size of each value. Total footprint is "
        "~num_keys × value_bytes plus key-name overhead.",
    )
    ttl_seconds: int = Field(
        default=60,
        ge=1,
        le=3600,
        description="TTL on every key — no permanent damage. Default 60s.",
    )


class SaturateRedisOutput(BaseModel):
    run_id: str
    key_prefix: str
    keys_written: int
    total_value_bytes: int
    ttl_seconds: int


@chaos_tool(
    "saturate_redis",
    description=(
        "Write a configurable number of keys to Redis to induce memory "
        "pressure and potential eviction. Every key expires after "
        "`ttl_seconds` (default 60) so the effect self-cleans. Watch "
        "`get_redis_health` for used_memory + evicted keys."
    ),
    input_model=SaturateRedisInput,
    output_model=SaturateRedisOutput,
    blast_radius=BlastRadius.SHARED_DEPENDENCY,
)
async def saturate_redis(
    inp: SaturateRedisInput, ctx: ToolContext
) -> SaturateRedisOutput:
    run_id = uuid.uuid4().hex[:12]
    prefix = f"chaos:sat:{run_id}"

    # One byte at value_bytes exactly — deterministic byte count so the
    # agent can reason about used_memory deltas.
    filler = b"x" * inp.value_bytes

    # Use a pipeline if available on the client to avoid `num_keys`
    # separate round trips; fall back to per-key SETs otherwise.
    pipe = getattr(ctx.redis, "pipeline", None)
    if pipe is not None:
        p = pipe()
        for i in range(inp.num_keys):
            p.set(f"{prefix}:{i}", filler, ex=inp.ttl_seconds)
        await p.execute()
    else:
        for i in range(inp.num_keys):
            await ctx.redis.set(f"{prefix}:{i}", filler, ex=inp.ttl_seconds)

    total = inp.num_keys * inp.value_bytes
    logger.warning(
        "chaos saturate_redis wrote keys",
        extra={
            "run_id": run_id,
            "num_keys": inp.num_keys,
            "total_bytes": total,
            "ttl_seconds": inp.ttl_seconds,
        },
    )
    return SaturateRedisOutput(
        run_id=run_id,
        key_prefix=prefix,
        keys_written=inp.num_keys,
        total_value_bytes=total,
        ttl_seconds=inp.ttl_seconds,
    )
