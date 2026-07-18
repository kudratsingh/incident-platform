"""
`pause_dag` — pause promotion of children in a job dependency DAG.

Mechanism: Redis key `dag:paused:<root_id>` with a TTL. The
DependencyResolver consumer checks the key before promoting a
`WAITING` child; if any ancestor (or the child itself) is paused,
the child stays waiting.

Real effect self-cleans on TTL. Companion to (future)
`resume_dag`; for now the effect ends when the TTL expires.

`actions:execute` + idempotent.
"""

import uuid

from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.repositories.job import JobRepository
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)


def pause_key_for(root_id: uuid.UUID) -> str:
    """Redis key the pause_dag tool sets. Consumers checking whether
    to promote a WAITING child probe this key on each ancestor id."""
    return f"dag:paused:{root_id}"


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
        "`root_job_id`. The DependencyResolver consumer checks the "
        "pause key before promoting; while set, no child promotes. "
        "Effect self-cleans on TTL (default 10 minutes). Idempotent."
    ),
    input_model=PauseDagInput,
    output_model=PauseDagOutput,
    required_scope=Scope.ACTIONS_EXECUTE,
    is_idempotent=True,
)
async def pause_dag(inp: PauseDagInput, ctx: ToolContext) -> PauseDagOutput:
    # Confirm the root exists + is in the caller's tenant. Same-shape
    # NotFoundError whether the row is missing or in a sibling tenant.
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
