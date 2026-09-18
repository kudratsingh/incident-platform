"""`saturate_redis` — write N keys of M bytes under `chaos:sat:{run_id}:{i}`.

Real effect: memory pressure, possibly evicting platform keys. The TTL bounds
only what this tool wrote, not what the eviction destroyed (WO-R2-56) — evicted
CQRS read-model keys need `read_model.rebuild_read_model`, while rate limits and
the backpressure cache rebuild themselves. `MAX_TOTAL_BYTES` caps the product,
not each dimension; `chaos:invoke` gates the tool.
"""

import uuid
from typing import Self

from app.core.logging import get_logger
from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

logger = get_logger(__name__)

# Ceiling on num_keys × value_bytes: enough to trip eviction on a lab Redis,
# far below the smallest instance's memory, so it is pressure and not an OOM.
MAX_TOTAL_BYTES = 256 * 1024 * 1024


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
        description="TTL on every key this tool writes, so its own footprint "
        "self-cleans. Keys it caused Redis to EVICT do not come back. "
        "Default 60s.",
    )

    @model_validator(mode="after")
    def _bound_total_footprint(self) -> Self:
        total = self.num_keys * self.value_bytes
        if total > MAX_TOTAL_BYTES:
            # PydanticCustomError, not a bare ValueError: the handler
            # json-encodes `exc.errors()`, and a ValueError's unserializable
            # `ctx` would turn this invalid-params refusal into a 500.
            raise PydanticCustomError(
                "footprint_too_large",
                "num_keys × value_bytes = {total} bytes exceeds the "
                "{cap}-byte cap on a single saturate_redis run. Lower either "
                "dimension; the per-field maxima bound each one alone, not "
                "the footprint they multiply out to.",
                {"total": total, "cap": MAX_TOTAL_BYTES},
            )
        return self


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
        "pressure and potential eviction. The keys written expire after "
        "`ttl_seconds` (default 60); keys they push OUT of Redis do not "
        "come back on their own. `num_keys × value_bytes` is capped at "
        "256 MiB per run. Watch `get_redis_health` for used_memory + "
        "evicted keys."
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
