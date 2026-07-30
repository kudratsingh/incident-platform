"""
`replay_dlq_by_category` — bulk replay one remediation category.

The agent's simplest branch: DLQ has one or more `replay_safe`
entries and the underlying fix is deployed → replay all of them at
once. The category filter guarantees the loop won't accidentally
touch `human_required` entries (which the platform refuses even if
the caller asks).

Categories accepted: `replay_safe`, `wait_and_replay`. The
`human_required` category is **refused** — persistent bugs need a
human review path, and auto-replaying them would just re-fail. If
the agent tries anyway, the tool returns an error before touching
any job.

`actions:execute` + idempotent. Bounded by `max_replays` (default
20, capped at 100).
"""

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.models.enums import JobStatus, RemediationHint
from app.repositories.audit import AuditRepository
from app.repositories.job import JobRepository
from app.repositories.job_dependency import JobDependencyRepository
from app.repositories.outbox import OutboxRepository
from app.services.job import JobService
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)

# Categories this tool is willing to replay. `human_required` is
# deliberately excluded — the whole point of that classification is
# that automatic replay is wrong.
_REPLAYABLE_CATEGORIES = frozenset(
    {
        RemediationHint.REPLAY_SAFE.value,
        RemediationHint.WAIT_AND_REPLAY.value,
    }
)


class ReplayDlqByCategoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(
        description=(
            "Which remediation category to replay. Must be "
            "`replay_safe` or `wait_and_replay`. `human_required` "
            "is refused — those need a human review path."
        )
    )
    job_type: str | None = Field(
        default=None,
        description="Optional narrowing to one job type in the category.",
    )
    max_replays: int = Field(default=20, ge=1, le=100)
    idempotency_key: str = Field(min_length=8, max_length=255)


class ReplayDlqByCategoryOutput(BaseModel):
    category: str
    matched: int
    replayed: int
    failed: int
    job_ids: list[str]


@tool(
    "replay_dlq_by_category",
    description=(
        "Bulk-replay every DLQ entry in one remediation category "
        "(`replay_safe` or `wait_and_replay`). Refuses "
        "`human_required` — those must go through a human review "
        "path. Bounded by `max_replays` (default 20). Idempotent."
    ),
    input_model=ReplayDlqByCategoryInput,
    output_model=ReplayDlqByCategoryOutput,
    required_scope=Scope.ACTIONS_EXECUTE,
    is_idempotent=True,
)
async def replay_dlq_by_category(
    inp: ReplayDlqByCategoryInput, ctx: ToolContext
) -> ReplayDlqByCategoryOutput:
    if inp.category not in _REPLAYABLE_CATEGORIES:
        raise ReplayDlqCategoryRefusedError(
            f"Category {inp.category!r} is not replayable. Allowed: "
            f"{sorted(_REPLAYABLE_CATEGORIES)}. `human_required` "
            "entries must go through a human review path."
        )

    job_repo = JobRepository(ctx.db)
    jobs, _ = await job_repo.list_jobs(
        tenant_id=ctx.principal.tenant_id,
        offset=0,
        limit=inp.max_replays,
        status=JobStatus.DEAD_LETTER.value,
        job_type=inp.job_type,
        remediation_hint=inp.category,
    )

    service = JobService(
        job_repo,
        AuditRepository(ctx.db),
        OutboxRepository(ctx.db),
        ctx.redis,
        dep_repo=JobDependencyRepository(ctx.db),
    )
    replayed_ids: list[str] = []
    failed = 0
    for job in jobs:
        try:
            await service.replay_job(
                job_id=job.id,
                requesting_user_id=ctx.principal.id,
                tenant_id=ctx.principal.tenant_id,
            )
            replayed_ids.append(str(job.id))
        except AppError as exc:
            failed += 1
            logger.warning(
                "replay_dlq_by_category per-job failure",
                extra={"job_id": str(job.id), "error": exc.message},
            )

    return ReplayDlqByCategoryOutput(
        category=inp.category,
        matched=len(jobs),
        replayed=len(replayed_ids),
        failed=failed,
        job_ids=replayed_ids,
    )


class ReplayDlqCategoryRefusedError(AppError):
    status_code = 400
    error_code = "dlq_category_refused"
