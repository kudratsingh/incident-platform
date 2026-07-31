"""
`replay_dlq_messages` — bulk replay of dead-lettered jobs.

Reads DLQ jobs (optionally filtered by `job_type`) and replays each
via the existing `JobService.replay_job` — the same code path the
admin UI's Replay button uses. Each replay resets `retry_count`,
clears `error_message`, re-publishes to `job.submitted` through the
outbox.

`actions:execute` + idempotent. The idempotency key covers the
entire batch: replaying the same batch twice with the same key is a
no-op. Same key + different filter is refused as key reuse.
"""

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.models.enums import JobStatus
from app.repositories.audit import AuditRepository
from app.repositories.job import JobRepository
from app.repositories.job_dependency import JobDependencyRepository
from app.repositories.outbox import OutboxRepository
from app.services.job import JobService
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)


class ReplayDlqMessagesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_type: str | None = Field(
        default=None,
        description="Restrict replay to one job type. Omit to include "
        "every dead-lettered job in the tenant.",
    )
    limit: int = Field(
        default=25,
        ge=1,
        le=200,
        description="Maximum number of jobs to replay in one call. "
        "Applied after the DB fetch.",
    )
    idempotency_key: str = Field(min_length=8, max_length=255)


class ReplayedJob(BaseModel):
    id: str
    type: str


class ReplayDlqMessagesOutput(BaseModel):
    requested: int
    replayed: int
    failed: int
    jobs: list[ReplayedJob]


@tool(
    "replay_dlq_messages",
    description=(
        "Replay dead-lettered jobs through the existing job-submission "
        "path — resets retry counts and re-publishes to job.submitted. "
        "Uses the same code path as the admin Replay button. Idempotent."
    ),
    input_model=ReplayDlqMessagesInput,
    output_model=ReplayDlqMessagesOutput,
    required_scope=Scope.ACTIONS_EXECUTE,
    is_idempotent=True,
)
async def replay_dlq_messages(
    inp: ReplayDlqMessagesInput, ctx: ToolContext
) -> ReplayDlqMessagesOutput:
    job_repo = JobRepository(ctx.db)
    audit_repo = AuditRepository(ctx.db)
    outbox_repo = OutboxRepository(ctx.db)
    dep_repo = JobDependencyRepository(ctx.db)

    jobs, _total = await job_repo.list_jobs(
        tenant_id=ctx.principal.tenant_id,
        offset=0,
        limit=inp.limit,
        status=JobStatus.DEAD_LETTER.value,
        job_type=inp.job_type,
    )

    service = JobService(
        job_repo, audit_repo, outbox_repo, ctx.redis, dep_repo=dep_repo
    )

    replayed: list[ReplayedJob] = []
    failed = 0
    for job in jobs:
        try:
            updated = await service.replay_job(
                job_id=job.id,
                tenant_id=ctx.principal.tenant_id,
                principal_type=ctx.principal.kind,
                principal_id=ctx.principal.id,
            )
            replayed.append(ReplayedJob(id=str(updated.id), type=updated.type))
        except AppError as exc:
            failed += 1
            logger.warning(
                "replay_dlq_messages replay failed",
                extra={"job_id": str(job.id), "error": exc.message},
            )

    return ReplayDlqMessagesOutput(
        requested=len(jobs),
        replayed=len(replayed),
        failed=failed,
        jobs=replayed,
    )
