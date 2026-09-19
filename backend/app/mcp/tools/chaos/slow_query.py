"""`slow_db_queries` — keep long queries running against one declared hot read relation.

Writes `chaos:db_query:slow`, which the worker's chaos-only sleeper task reads once a second
(`app/workers/db_slow_query.py`, ADR 0034); teardown is the TTL plus the reset's `chaos:*` sweep.
Queries run long while the pool that answers a health reading stays normal — the mirror image of
`saturate_db_pool`, and the pair is what tells the two faults apart.
"""

from enum import StrEnum

from app.core.logging import get_logger
from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext
from app.mcp.tools.health import SLOW_QUERY_THRESHOLD_MS
from app.workers.db_slow_query import (
    DEFAULT_QUERY_MS,
    MAX_QUERY_MS,
    MIN_QUERY_MS,
    POLL_INTERVAL_SECONDS,
    SLEEPER_COUNT,
    TARGET_RELATIONS,
    encode_request,
    slow_query_key,
)
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)

__all__ = ["SlowQueryTarget", "slow_db_queries", "slow_query_key"]


# The declared scope, as a closed set: a target outside it is refused as invalid params rather
# than accepted and matched against nothing. Every member is a key of `TARGET_RELATIONS`, which
# is asserted by the tests — the map is what turns a member into a relation name.
class SlowQueryTarget(StrEnum):
    JOB_READS = "job_reads"
    AUDIT_READS = "audit_reads"
    OUTBOX_READS = "outbox_reads"


class SlowQueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: SlowQueryTarget = Field(
        default=SlowQueryTarget.JOB_READS,
        description=(
            "Which read path runs slowly: `job_reads` (default), "
            "`audit_reads` or `outbox_reads`. Each names one relation, and "
            "the slow query really reads it, so it holds that relation's read "
            "lock while it runs. Anything else is refused."
        ),
    )
    query_ms: int = Field(
        default=DEFAULT_QUERY_MS,
        ge=MIN_QUERY_MS,
        le=MAX_QUERY_MS,
        description=(
            f"How long each query takes, in milliseconds. The floor of "
            f"{MIN_QUERY_MS} is above twice the platform's "
            f"{int(SLOW_QUERY_THRESHOLD_MS)} ms slow-query threshold on "
            f"purpose: with {SLEEPER_COUNT} queries offset by half this, the "
            "longest one running is always past that threshold, so the "
            "reading never dips back to normal between queries. The ceiling "
            f"of {MAX_QUERY_MS} bounds how long one query can outlive the "
            "flag."
        ),
    )
    ttl_seconds: int = Field(
        default=300,
        ge=1,
        le=3600,
        description=(
            "How long queries keep running slowly. They stop on the first "
            "pass after it expires — nothing has to be called. Default 5 "
            "minutes."
        ),
    )


class SlowQueryOutput(BaseModel):
    flag_key: str
    target: str
    relation: str = Field(
        description="The relation `target` names, which the slow query reads."
    )
    query_ms: int
    ttl_seconds: int
    concurrent_queries: int = Field(
        description=(
            "How many slow queries run at once, offset within `query_ms` so "
            "one is always past the slow threshold. Fixed, not a caller "
            "argument: it is also how many connections this takes out of the "
            "worker's pool, and the free floor is what caps it."
        )
    )
    poll_interval_seconds: float = Field(
        description=(
            "Seconds between the task's passes. The fault starts, changes and "
            "ends on a pass, so a ttl_seconds smaller than this expires "
            "before any query runs."
        )
    )
    slow_query_threshold_ms: float = Field(
        description=(
            "The platform's own threshold, for comparison with `query_ms`: "
            "the reading that counts queries past it is what this fault "
            "moves."
        )
    )
    accepted: bool = Field(
        description=(
            "True if the flag was set. This does not confirm a query is "
            "running yet — that happens on the task's next pass, at most "
            "poll_interval_seconds away, plus the first query's own offset."
        )
    )


@chaos_tool(
    "slow_db_queries",
    description=(
        "Keep queries against one declared read path running long — "
        "`query_ms` each (default 2000, min 1100, max 10000), "
        "`concurrent_queries` at a time, for `ttl_seconds` (default 300, max "
        "3600). The queries run in the API and worker process and are real "
        "reads of the relation `target` names, so the database server sees "
        "them: `get_postgres_health` reports them in "
        "`longest_active_query_ms` and `active_queries_over_slow_threshold`, "
        "which are read from the server and therefore the same answer "
        "whichever process asks. Its `pool_*` counters are NOT moved: this "
        "takes `concurrent_queries` connections out of the worker's pool and "
        "always leaves the free floor `saturate_db_pool` keeps, and a pool "
        "reading is per-process anyway. That contrast is the point — slow "
        "queries with a healthy pool, against `saturate_db_pool`'s full pool "
        "with normal queries. `p95_query_ms_1m` stays null: the platform "
        "keeps no per-minute query history and this does not give it one. "
        "What it does not do: the platform's own jobs still run at their "
        "normal speed, so job durations, consumer lag and the SLO readings "
        "are unaffected. Every query stops when the flag expires, when the "
        "environment reset clears it, or when the worker restarts; one "
        "already-running query finishes first, so up to `query_ms` outlives "
        "the flag. A repeat call replaces the fault instead of adding a "
        "second one."
    ),
    input_model=SlowQueryInput,
    output_model=SlowQueryOutput,
    blast_radius=BlastRadius.SHARED_DEPENDENCY,
)
async def slow_db_queries(
    inp: SlowQueryInput, ctx: ToolContext
) -> SlowQueryOutput:
    key = slow_query_key()
    # The task parses this exact form, and looks the target up in its own map rather than
    # interpolating it.
    await ctx.redis.set(
        key, encode_request(inp.target.value, inp.query_ms), ex=inp.ttl_seconds
    )
    logger.warning(
        "chaos slow_db_queries set",
        extra={
            "tenant_id": str(ctx.principal.tenant_id),
            "target": inp.target.value,
            "query_ms": inp.query_ms,
            "ttl_seconds": inp.ttl_seconds,
        },
    )
    return SlowQueryOutput(
        flag_key=key,
        target=inp.target.value,
        relation=TARGET_RELATIONS[inp.target.value],
        query_ms=inp.query_ms,
        ttl_seconds=inp.ttl_seconds,
        concurrent_queries=SLEEPER_COUNT,
        poll_interval_seconds=POLL_INTERVAL_SECONDS,
        slow_query_threshold_ms=SLOW_QUERY_THRESHOLD_MS,
        accepted=True,
    )
