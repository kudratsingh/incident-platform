"""`pause_dag_chaos` — a lab DAG pause indistinguishable from an operator's:
the platform's own `dag:paused:<root_id>`, written through the imported
`app/utils/dag_pause.pause_key_for` with `pause_dag`'s value, TTL default and
bounds, so `get_dag_state` cannot tell them apart (ADR 0012 rule 1, ADR 0029).
The key sits outside `chaos:*` on purpose, so teardown is the TTL plus the
reset's `_clear_dag_pauses`. Exists because `pause_dag` needs `actions:execute`.
"""

import uuid

from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext
from app.repositories.job import JobRepository
from app.utils.dag_pause import pause_key_for
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)

__all__ = ["pause_dag_chaos", "pause_key_for"]


class PauseDagChaosInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_job_id: uuid.UUID = Field(
        description="Root of the DAG to pause. Any WAITING child whose "
        "chain of parents reaches this root won't be promoted while the "
        "flag is set."
    )
    ttl_seconds: int = Field(
        default=600,
        ge=1,
        le=3600,
        description="How long the pause is active. Default 10 minutes — "
        "the same default and the same bounds the operator action uses, "
        "so a pause taken here with default arguments is identical to one "
        "an operator took.",
    )


class PauseDagChaosOutput(BaseModel):
    root_job_id: str
    pause_key: str
    ttl_seconds: int
    accepted: bool


@chaos_tool(
    "pause_dag_chaos",
    description=(
        "Pause promotion of WAITING children in the DAG rooted at "
        "`root_job_id`, producing a pause that is indistinguishable from "
        "an operator pause: the same Redis flag `pause_dag` writes, with a "
        "TTL, so `get_dag_state(root_job_id)` reads `paused: true` with "
        "`paused_expires_in_seconds`, a held descendant names this root as "
        "its `paused_by`, and children stay in status `waiting`. Nothing "
        "in what a reader gets back says the pause came from here. "
        "Self-cleans on TTL (default 600 s, max 3600), after which held "
        "children promote automatically, so the fault is bounded with "
        "nothing to call; the environment reset also clears every DAG "
        "pause. Exists because `pause_dag` needs `actions:execute`. Use "
        "`pause_control_loop` to stop a background loop instead — that is "
        "a different fault with the opposite meaning."
    ),
    input_model=PauseDagChaosInput,
    output_model=PauseDagChaosOutput,
    blast_radius=BlastRadius.ENVIRONMENT_WIDE,
)
async def pause_dag_chaos(
    inp: PauseDagChaosInput, ctx: ToolContext
) -> PauseDagChaosOutput:
    # Same check and error as the operator action: a mistyped root must
    # not report a fault nothing injected.
    job_repo = JobRepository(ctx.db)
    root = await job_repo.get_by_id(inp.root_job_id)
    if root is None or root.tenant_id != ctx.principal.tenant_id:
        raise NotFoundError(f"job not found: {inp.root_job_id}")

    key = pause_key_for(inp.root_job_id)
    # Must stay this exact string, or an operator reading Redis mid-run
    # would find the lab in it.
    await ctx.redis.set(key, "paused", ex=inp.ttl_seconds)
    logger.warning(
        "chaos pause_dag_chaos injected",
        extra={
            "tenant_id": str(ctx.principal.tenant_id),
            "root_id": str(inp.root_job_id),
            "ttl_seconds": inp.ttl_seconds,
        },
    )
    return PauseDagChaosOutput(
        root_job_id=str(inp.root_job_id),
        pause_key=key,
        ttl_seconds=inp.ttl_seconds,
        accepted=True,
    )
