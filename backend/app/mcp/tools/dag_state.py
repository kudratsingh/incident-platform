"""
`get_dag_state` — the job dependency graph around one job.

Traverses `job_dependencies` outward from the seed job (parents +
children, one hop each direction — enough to explain "why is this
stuck?"). Nodes carry status so the agent can immediately see which
upstream/downstream jobs are the interesting ones.

Also reports DAG pause state, which is how `pause_dag` is verified:
`paused` is the seed's own flag, `paused_by` names the ancestor whose
flag is holding the seed back (they differ when a pause was applied
further up the chain).

Requires `incidents:read`. Tenant-scoped through `JobRepository`.
"""

import uuid
from datetime import datetime

from app.core.exceptions import NotFoundError
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.repositories.job import JobRepository
from app.repositories.job_dependency import JobDependencyRepository
from app.utils.dag_pause import find_blocking_pause, pause_state
from pydantic import BaseModel, ConfigDict, Field


class GetDagStateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID = Field(
        description="Root job. Its parents (dependencies) and children "
        "(dependents) are traversed one hop each direction."
    )


class DagNode(BaseModel):
    id: str
    type: str
    status: str
    retry_count: int
    created_at: datetime


class DagEdge(BaseModel):
    """`from_id` depends on `to_id` — the parent has to finish before
    the child can start. Direction matches the schema
    (`job_dependencies.child_job_id → parent_job_id`)."""

    from_id: str
    to_id: str


class GetDagStateOutput(BaseModel):
    # Explicit title only. The property name stays `seed_id` — renaming it
    # would be a breaking output-contract change — but Pydantic's derived
    # title ("Seed Id") is serialized into `outputSchema` and reads as lab
    # vocabulary on the wire. Titling it after the graph sense keeps the
    # ADR 0012 screen strict with no allowlist.
    seed_id: str = Field(title="Root Job Id")
    nodes: list[DagNode]
    edges: list[DagEdge]
    paused: bool = Field(
        description="True when this job carries its own pause flag — the "
        "direct result of pause_dag(root_job_id=this job)."
    )
    paused_expires_in_seconds: int | None = Field(
        default=None,
        description="Seconds until this job's own pause flag expires, and "
        "held children resume automatically. Null when not paused (or "
        "when the flag carries no expiry).",
    )
    paused_by: str | None = Field(
        default=None,
        description="Job id whose pause flag is holding this job back — "
        "itself, or an ancestor when the pause was applied further up "
        "the chain. Null when nothing in the ancestry is paused.",
    )


@tool(
    "get_dag_state",
    description=(
        "Fetch the immediate dependency graph around one job — direct "
        "parents and direct children with statuses. Use to answer "
        "'why is this job waiting?' or 'what will unblock if I replay?' "
        "Also reports pause state: `paused` is this job's own flag, "
        "`paused_by` names the ancestor holding it back, and "
        "`paused_expires_in_seconds` counts down to automatic resume. "
        "This is the verification surface for pause_dag — a successful "
        "pause reads as paused=true with children still in `waiting`. "
        "Live read: graph and statuses come from Postgres, pause flags "
        "from Redis; both reflect the moment of the call."
    ),
    input_model=GetDagStateInput,
    output_model=GetDagStateOutput,
    required_scope=Scope.INCIDENTS_READ,
)
async def get_dag_state(
    inp: GetDagStateInput, ctx: ToolContext
) -> GetDagStateOutput:
    job_repo = JobRepository(ctx.db)
    dep_repo = JobDependencyRepository(ctx.db)

    seed = await job_repo.get_by_id(inp.job_id)
    if seed is None or seed.tenant_id != ctx.principal.tenant_id:
        raise NotFoundError(f"job not found: {inp.job_id}")

    parent_ids = await dep_repo.parents(seed.id)
    child_ids = await dep_repo.children_of(seed.id)

    # One SELECT round-trip per node is fine at this scale — the DAG
    # around any single job is bounded (a saga is a chain; ad-hoc deps
    # are rare and shallow).
    nodes: dict[str, DagNode] = {}

    async def _load(job_id: uuid.UUID) -> None:
        j = await job_repo.get_by_id(job_id)
        if j is None or j.tenant_id != ctx.principal.tenant_id:
            return
        nodes[str(j.id)] = DagNode(
            id=str(j.id),
            type=j.type,
            status=j.status,
            retry_count=j.retry_count,
            created_at=j.created_at,
        )

    await _load(seed.id)
    for pid in parent_ids:
        await _load(pid)
    for cid in child_ids:
        await _load(cid)

    edges: list[DagEdge] = []
    for pid in parent_ids:
        edges.append(DagEdge(from_id=str(seed.id), to_id=str(pid)))
    for cid in child_ids:
        edges.append(DagEdge(from_id=str(cid), to_id=str(seed.id)))

    paused, expires_in = await pause_state(ctx.redis, seed.id)
    blocking = await find_blocking_pause(ctx.redis, dep_repo, seed.id)

    return GetDagStateOutput(
        seed_id=str(seed.id),
        nodes=list(nodes.values()),
        edges=edges,
        paused=paused,
        paused_expires_in_seconds=expires_in,
        paused_by=str(blocking) if blocking is not None else None,
    )
