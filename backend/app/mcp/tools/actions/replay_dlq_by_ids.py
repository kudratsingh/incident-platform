"""
`replay_dlq_by_ids` — targeted DLQ replay.

The agent's remediation planner picks specific DLQ entries after
reading `list_dlq_messages` + `list_dlq_messages(remediation_hint=...)`
+ triage. This tool replays only those, avoiding the blast radius of
the coarser `replay_dlq_messages(job_type=, limit=)` when the agent
knows exactly which IDs are safe.

Each replay goes through the existing `JobService.replay_job` path —
resets `retry_count`, clears `error_message`, re-publishes to
`job.submitted` via the outbox. Per-id success/error is returned so
a partial replay is visible to the caller.

`actions:execute` + idempotent (via the standard dispatch wrapper).
"""

import uuid

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.repositories.audit import AuditRepository
from app.repositories.job import JobRepository
from app.repositories.job_dependency import JobDependencyRepository
from app.repositories.outbox import OutboxRepository
from app.services.job import JobService
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)


class ReplayDlqByIdsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_ids: list[uuid.UUID] = Field(
        min_length=1,
        max_length=50,
        description="Explicit job IDs to replay. Cap of 50 per call — "
        "the agent should chunk larger sets across multiple calls with "
        "distinct idempotency_keys.",
    )
    idempotency_key: str = Field(min_length=8, max_length=255)


class ReplayResult(BaseModel):
    id: str
    ok: bool
    error: str | None = None


class ReplayDlqByIdsOutput(BaseModel):
    requested: int
    replayed: int
    failed: int
    results: list[ReplayResult]


@tool(
    "replay_dlq_by_ids",
    description=(
        "Replay a specific set of DLQ jobs by ID. Safer than "
        "`replay_dlq_messages(job_type, limit)` when the agent has "
        "already picked entries from `list_dlq_messages`. Each replay "
        "hits the existing JobService path (reset retry_count, "
        "re-publish to job.submitted via the outbox). Returns "
        "per-id success + error so a partial replay is observable. "
        "Idempotent."
    ),
    input_model=ReplayDlqByIdsInput,
    output_model=ReplayDlqByIdsOutput,
    required_scope=Scope.ACTIONS_EXECUTE,
    is_idempotent=True,
)
async def replay_dlq_by_ids(
    inp: ReplayDlqByIdsInput, ctx: ToolContext
) -> ReplayDlqByIdsOutput:
    service = JobService(
        JobRepository(ctx.db),
        AuditRepository(ctx.db),
        OutboxRepository(ctx.db),
        ctx.redis,
        dep_repo=JobDependencyRepository(ctx.db),
    )
    results: list[ReplayResult] = []
    replayed = 0
    failed = 0
    for job_id in inp.job_ids:
        try:
            await service.replay_job(
                job_id=job_id,
                requesting_user_id=ctx.principal.id,
                tenant_id=ctx.principal.tenant_id,
            )
            results.append(ReplayResult(id=str(job_id), ok=True))
            replayed += 1
        except AppError as exc:
            failed += 1
            results.append(
                ReplayResult(id=str(job_id), ok=False, error=exc.message)
            )
            logger.warning(
                "replay_dlq_by_ids per-id failure",
                extra={"job_id": str(job_id), "error": exc.message},
            )

    return ReplayDlqByIdsOutput(
        requested=len(inp.job_ids),
        replayed=replayed,
        failed=failed,
        results=results,
    )
