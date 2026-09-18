"""
`pause_dag` — pause promotion of children in a job dependency DAG.

Sets Redis `dag:paused:<root_id>` with a TTL, checked at six dispatch points plus a
create-time hold (ADR 0011 and its 2026-08-09 amendment have the table and the
refuse-vs-defer split). Work already `RUNNING` is not recalled, and
`_resume_unblocked_waiting_loop` promotes held children once the TTL lapses, so a
pause is temporary. Verify with `get_dag_state.paused`. `actions:execute` + idempotent.
"""

import uuid

from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.repositories.job import JobRepository
from app.utils.dag_pause import pause_key_for
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)

__all__ = ["pause_dag", "pause_key_for"]


class PauseDagInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_job_id: uuid.UUID = Field(
        description="Root of the DAG to pause. Any WAITING child whose "
        "chain of parents reaches this root won't be promoted while "
        "the flag is set."
    )
    ttl_seconds: int = Field(
        default=600,
        ge=1,
        le=3600,
        description="How long the pause is active. Default 10 minutes.",
    )
    idempotency_key: str = Field(min_length=8, max_length=255)


class PauseDagOutput(BaseModel):
    root_job_id: str
    pause_key: str
    ttl_seconds: int
    accepted: bool


@tool(
    "pause_dag",
    description=(
        "Pause promotion of WAITING children in the DAG rooted at "
        "`root_job_id`. Observable effect: `get_dag_state(root_job_id)` "
        "returns `paused=true` with `paused_expires_in_seconds`, "
        "children stay in status `waiting`, and no new promotions "
        "occur while the flag is set. Already-RUNNING jobs are not "
        "cancelled — pause stops promotion, it does not stop work in "
        "flight. Self-cleans on TTL (default 10 minutes), after which "
        "held children promote automatically. Idempotent.\n"
        "STABILIZER, NOT A FIX: pausing changes nothing about the node "
        "that stopped the chain. When the TTL expires the held children "
        "promote back into the same stalled state. It also blocks the "
        "fix while it holds — the platform refuses to replay any job "
        "inside a paused DAG. Use it to stop promotion while a human "
        "decides, never as a remediation."
    ),
    input_model=PauseDagInput,
    output_model=PauseDagOutput,
    required_scope=Scope.ACTIONS_EXECUTE,
    is_idempotent=True,
)
async def pause_dag(inp: PauseDagInput, ctx: ToolContext) -> PauseDagOutput:
    # Same-shape NotFoundError whether the row is missing or in a sibling tenant.
    job_repo = JobRepository(ctx.db)
    root = await job_repo.get_by_id(inp.root_job_id)
    if root is None or root.tenant_id != ctx.principal.tenant_id:
        raise NotFoundError(f"job not found: {inp.root_job_id}")

    key = pause_key_for(inp.root_job_id)
    await ctx.redis.set(key, "paused", ex=inp.ttl_seconds)
    logger.warning(
        "action pause_dag",
        extra={"root_id": str(inp.root_job_id), "ttl_seconds": inp.ttl_seconds},
    )
    return PauseDagOutput(
        root_job_id=str(inp.root_job_id),
        pause_key=key,
        ttl_seconds=inp.ttl_seconds,
        accepted=True,
    )
