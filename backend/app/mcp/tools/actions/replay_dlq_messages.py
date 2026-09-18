"""
`replay_dlq_messages` — bulk replay of dead-lettered jobs.

Replays each through `JobService.replay_job`, the same path as the admin Replay
button. `human_required` entries are excluded by default (R2-22): this is the *blind*
tool, so nobody looked at the individual rows, and replaying them re-runs a known bug
under a fresh failure. `include_human_required=true` is the explicit consent, as
enumerating ids is for `replay_dlq_by_ids`. `actions:execute` + idempotent, with the
key covering the whole batch — same key + different filter is refused as key reuse.
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

# The one category the blind batch will not take by default. Deny-list, not the
# allow-list `replay_dlq_by_category` uses: uncategorised is unclassified, not fenced.
_FENCED_CATEGORY = RemediationHint.HUMAN_REQUIRED.value


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
    include_human_required: bool = Field(
        default=False,
        description=(
            "Include `human_required` entries in the batch. Default "
            "false: they are left in the DLQ and reported under "
            "`skipped_human_required` / `skipped_jobs`. That category "
            "means a persistent bug — a blind replay just re-fails it. "
            "Set true only once the underlying bug is fixed and you "
            "mean these specific entries; otherwise resolve them "
            "individually or escalate."
        ),
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
    # Reported, not dropped: an agent that cannot see why keeps re-running.
    skipped_human_required: int = 0
    skipped_jobs: list[ReplayedJob] = []


@tool(
    "replay_dlq_messages",
    description=(
        "Replay dead-lettered jobs through the existing job-submission "
        "path — resets retry counts and re-publishes to job.submitted. "
        "Uses the same code path as the admin Replay button. Idempotent.\n"
        "BLAST RADIUS: `human_required` entries are NOT replayed. They "
        "stay in the DLQ and come back under `skipped_human_required` "
        "and `skipped_jobs`, so a DLQ that has not fully drained after "
        "this call is the expected result, not a failed replay — "
        "re-running the tool will not clear them. That category means a "
        "persistent bug that replaying only re-triggers; resolve those "
        "entries individually or escalate. Pass "
        "`include_human_required=true` to replay them anyway, once the "
        "underlying bug is fixed.\n"
        "Entries with no remediation category are still replayed: "
        "uncategorised means triage has not classified the failure yet, "
        "not that it is fenced."
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

    # A query filter, not a post-fetch skip, so a batch of `limit` still returns
    # `limit` replayable jobs.
    fenced: tuple[str, ...] = (
        () if inp.include_human_required else (_FENCED_CATEGORY,)
    )
    jobs, _total = await job_repo.list_jobs(
        tenant_id=ctx.principal.tenant_id,
        offset=0,
        limit=inp.limit,
        status=JobStatus.DEAD_LETTER.value,
        job_type=inp.job_type,
        exclude_remediation_hints=fenced,
    )

    # Second query: the exclusion above leaves fenced rows out of `jobs`, and the
    # caller still needs to know they exist.
    skipped: list[ReplayedJob] = []
    if fenced:
        fenced_jobs, _ = await job_repo.list_jobs(
            tenant_id=ctx.principal.tenant_id,
            offset=0,
            limit=inp.limit,
            status=JobStatus.DEAD_LETTER.value,
            job_type=inp.job_type,
            remediation_hint=_FENCED_CATEGORY,
        )
        skipped = [
            ReplayedJob(id=str(job.id), type=job.type) for job in fenced_jobs
        ]

    service = JobService(
        job_repo, audit_repo, outbox_repo, ctx.redis, dep_repo=dep_repo
    )

    replayed: list[ReplayedJob] = []
    failed = 0
    for job in jobs:
        # SAVEPOINT per item (#5): a mid-loop exception rolls back only this
        # iteration. Without it, a non-AppError on job N returned "internal tool
        # error" while the writes for jobs 1..N-1 still committed.
        try:
            async with ctx.db.begin_nested():
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
        except Exception as exc:
            # Count as failed but keep going; the savepoint rolled this item back.
            failed += 1
            logger.exception(
                "replay_dlq_messages replay crashed",
                extra={"job_id": str(job.id), "error": str(exc)},
            )

    return ReplayDlqMessagesOutput(
        requested=len(jobs),
        replayed=len(replayed),
        failed=failed,
        jobs=replayed,
        skipped_human_required=len(skipped),
        skipped_jobs=skipped,
    )
