"""`degrade_downstream` — make the simulated bulk-api-sync endpoints fail, or answer late.

Writes `chaos:downstream:bulk_api_sync`, which `process_bulk_api_sync` reads once per job
(`app/workers/async_tasks.py`); under `fail` the shipped `bulk-api-sync` breaker crosses its
threshold and opens, then cycles OPEN → HALF_OPEN → OPEN every recovery window while the flag
is set. Teardown is the TTL plus the reset's `chaos:*` sweep (ADR 0031).
"""

from typing import Literal

from app.core.logging import get_logger
from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext
from app.workers.async_tasks import (
    DEGRADE_SLOW,
    bulk_api_breaker,
    downstream_flag_key,
)
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)

__all__ = ["degrade_downstream", "downstream_flag_key"]

#: Enough latency to be obvious without reaching the job execution deadline (600 s).
MAX_DELAY_MS = 30_000


class DegradeDownstreamInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The two literals are `async_tasks.DEGRADE_FAIL` / `DEGRADE_SLOW`; written out because a
    # `str` constant is not a `Literal` to the type checker. Pinned by the tests.
    mode: Literal["fail", "slow"] = Field(
        default="fail",
        description=(
            "`fail`: every endpoint call returns 503, which the breaker "
            "counts, so it opens. `slow`: every call answers `delay_ms` late "
            "and succeeds, so the breaker stays closed and only the job's "
            "duration moves."
        ),
    )
    delay_ms: int = Field(
        default=2000,
        ge=1,
        le=MAX_DELAY_MS,
        description=(
            "Milliseconds each endpoint call takes under `mode=slow`; "
            "ignored under `fail`. Capped at 30000, below the job execution "
            "deadline, so a slow dependency does not read as a wedged worker."
        ),
    )
    ttl_seconds: int = Field(
        default=300,
        ge=1,
        le=3600,
        description=(
            "How long the dependency stays degraded. Calls go back to normal "
            "on the first job after it expires — nothing has to be called. "
            "Default 5 minutes."
        ),
    )


class DegradeDownstreamOutput(BaseModel):
    dependency: str
    flag_key: str
    mode: str
    delay_ms: int
    ttl_seconds: int
    failure_threshold: int = Field(
        description=(
            "Consecutive failures that open the breaker, read from the "
            "breaker itself: the first job syncing this many endpoints under "
            "`mode=fail` opens it."
        )
    )
    recovery_timeout_seconds: float = Field(
        description=(
            "Seconds the breaker waits before admitting one probe. While the "
            "flag is set that probe fails and it re-opens, so a reader that "
            "samples once may catch either state."
        )
    )
    accepted: bool


@chaos_tool(
    "degrade_downstream",
    description=(
        "Degrade the simulated third-party endpoints behind the "
        "`bulk-api-sync` circuit breaker for `ttl_seconds` (default 300, max "
        "3600): `mode=fail` (default) makes every endpoint call return 503, "
        "`mode=slow` makes each one answer `delay_ms` late but succeed. Under "
        "`fail` the breaker opens once `failure_threshold` calls have failed, "
        "then admits one probe every `recovery_timeout_seconds`, fails it and "
        "re-opens — so a reader must poll across a recovery window rather "
        "than sample once, because either `open` or `half_open` is a correct "
        "reading. Also under `fail`, a job whose every endpoint call failed "
        "is itself failed, so the failure reaches the job surface: retries "
        "first, then the dead-letter queue. Under `slow` nothing fails and "
        "the breaker stays closed. Self-cleans when the flag expires, and "
        "the environment reset clears it; the breaker closes by itself on the "
        "first probe that succeeds afterwards. Only the `bulk_api_sync` job "
        "type is affected — other job types, the database and Redis are "
        "untouched."
    ),
    input_model=DegradeDownstreamInput,
    output_model=DegradeDownstreamOutput,
    blast_radius=BlastRadius.SINGLE_SERVICE,
)
async def degrade_downstream(
    inp: DegradeDownstreamInput, ctx: ToolContext
) -> DegradeDownstreamOutput:
    breaker = bulk_api_breaker()
    key = downstream_flag_key()
    # The processor parses this exact form; `slow` carries its delay, `fail` ignores it.
    await ctx.redis.set(key, f"{inp.mode}:{inp.delay_ms}", ex=inp.ttl_seconds)
    logger.warning(
        "chaos degrade_downstream set",
        extra={
            "tenant_id": str(ctx.principal.tenant_id),
            "mode": inp.mode,
            "delay_ms": inp.delay_ms,
            "ttl_seconds": inp.ttl_seconds,
        },
    )
    return DegradeDownstreamOutput(
        dependency=breaker.name,
        flag_key=key,
        mode=inp.mode,
        delay_ms=inp.delay_ms if inp.mode == DEGRADE_SLOW else 0,
        ttl_seconds=inp.ttl_seconds,
        failure_threshold=breaker.failure_threshold,
        recovery_timeout_seconds=breaker.recovery_timeout,
        accepted=True,
    )
