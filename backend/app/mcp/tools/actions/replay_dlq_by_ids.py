"""
`replay_dlq_by_ids` — targeted DLQ replay, immediate or scheduled.

The agent's remediation planner picks specific DLQ entries after
reading `list_dlq_messages` + `list_dlq_messages(remediation_hint=...)`
+ triage. This tool replays only those, avoiding the blast radius of
the coarser `replay_dlq_messages(job_type=, limit=)` when the agent
knows exactly which IDs are safe.

If `delay_seconds` is omitted the replay fires immediately through
the standard `JobService.replay_job` path (reset retry_count, clear
error_message, republish via outbox). If set, each targeted job is
pushed onto the `jobs:dlq_replay_delayed` sorted set with score =
now + delay; the worker's promote loop fires them at their scheduled
time. That's the `wait_and_replay` remediation category: give the
transient dependency time to recover before retrying.

`actions:execute` + idempotent (via the standard dispatch wrapper).
"""

import uuid

from app.core.exceptions import AppError, NotFoundError
from app.core.logging import get_logger, request_id_var
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.models.enums import JobStatus
from app.repositories.audit import AuditRepository
from app.repositories.job import JobRepository
from app.repositories.job_dependency import JobDependencyRepository
from app.repositories.outbox import OutboxRepository
from app.services.job import JobService
from app.workers import dlq_replay_scheduler
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
    delay_seconds: int | None = Field(
        default=None,
        ge=1,
        le=3600,
        description=(
            "If set, the platform defers the enqueue for this many seconds "
            "before re-publishing to `job.submitted`. Use for "
            "`wait_and_replay` category entries where an external "
            "dependency needs time to recover. Omit for immediate replay. "
            "Cap of 1 hour keeps scheduled work from lingering past the "
            "incident's natural lifecycle."
        ),
    )
    idempotency_key: str = Field(min_length=8, max_length=255)


class ReplayResult(BaseModel):
    id: str
    ok: bool
    error: str | None = None
    # Set only when `delay_seconds` was passed. `execute_at` is the
    # epoch second the promote loop will fire the actual replay.
    scheduled: bool = False
    execute_at: float | None = None


class ReplayDlqByIdsOutput(BaseModel):
    requested: int
    replayed: int
    scheduled: int = 0
    failed: int
    results: list[ReplayResult]


@tool(
    "replay_dlq_by_ids",
    description=(
        "Replay a specific set of DLQ jobs by ID. Safer than "
        "`replay_dlq_messages(job_type, limit)` when the agent has "
        "already picked entries from `list_dlq_messages`. Immediate "
        "replay hits the existing JobService path (reset retry_count, "
        "re-publish to job.submitted via the outbox). Pass "
        "`delay_seconds` (1..3600) to defer the enqueue for the "
        "`wait_and_replay` category — the platform schedules on a "
        "Redis ZSET and a worker loop fires the replay when the "
        "delay elapses. Returns per-id outcome so a partial replay "
        "is observable. Idempotent.\n"
        "VERIFYING A DELAYED REPLAY: a scheduled entry is not replayed "
        "yet. It stays in status `dead_letter` and keeps appearing in "
        "list_dlq_messages until `execute_at` passes — an unchanged "
        "DLQ is the expected state, not a failure. Verify with the "
        "`scheduled` outcomes in this response and the "
        "`job.replay_scheduled` audit events, not with DLQ shrink. "
        "Re-check DLQ size only after `execute_at`."
    ),
    input_model=ReplayDlqByIdsInput,
    output_model=ReplayDlqByIdsOutput,
    required_scope=Scope.ACTIONS_EXECUTE,
    is_idempotent=True,
)
async def replay_dlq_by_ids(
    inp: ReplayDlqByIdsInput, ctx: ToolContext
) -> ReplayDlqByIdsOutput:
    job_repo = JobRepository(ctx.db)
    audit_repo = AuditRepository(ctx.db)
    service = JobService(
        job_repo,
        audit_repo,
        OutboxRepository(ctx.db),
        ctx.redis,
        dep_repo=JobDependencyRepository(ctx.db),
    )

    results: list[ReplayResult] = []
    replayed = 0
    scheduled = 0
    failed = 0

    for job_id in inp.job_ids:
        if inp.delay_seconds is None:
            # SAVEPOINT per item (#5) — same rationale as the sibling
            # replay tools. Immediate replay writes go through the
            # session; without a nested transaction, a mid-loop
            # non-AppError commits the earlier ids' writes behind an
            # error response.
            try:
                async with ctx.db.begin_nested():
                    await service.replay_job(
                        job_id=job_id,
                        tenant_id=ctx.principal.tenant_id,
                        principal_type=ctx.principal.kind,
                        principal_id=ctx.principal.id,
                    )
                results.append(ReplayResult(id=str(job_id), ok=True))
                replayed += 1
            except AppError as exc:
                failed += 1
                results.append(
                    ReplayResult(
                        id=str(job_id), ok=False, error=exc.message
                    )
                )
                logger.warning(
                    "replay_dlq_by_ids per-id failure",
                    extra={"job_id": str(job_id), "error": exc.message},
                )
            except Exception as exc:
                failed += 1
                results.append(
                    ReplayResult(
                        id=str(job_id), ok=False, error=str(exc)
                    )
                )
                logger.exception(
                    "replay_dlq_by_ids per-id crashed",
                    extra={"job_id": str(job_id), "error": str(exc)},
                )
            continue

        # Scheduled branch — pre-validate then push onto the ZSET.
        try:
            execute_at = await _schedule_one(
                job_id=job_id,
                delay_seconds=inp.delay_seconds,
                ctx=ctx,
                job_repo=job_repo,
                audit_repo=audit_repo,
            )
        except AppError as exc:
            failed += 1
            results.append(
                ReplayResult(id=str(job_id), ok=False, error=exc.message)
            )
            logger.warning(
                "replay_dlq_by_ids scheduled per-id failure",
                extra={"job_id": str(job_id), "error": exc.message},
            )
            continue

        results.append(
            ReplayResult(
                id=str(job_id),
                ok=True,
                scheduled=True,
                execute_at=execute_at,
            )
        )
        scheduled += 1

    return ReplayDlqByIdsOutput(
        requested=len(inp.job_ids),
        replayed=replayed,
        scheduled=scheduled,
        failed=failed,
        results=results,
    )


async def _schedule_one(
    *,
    job_id: uuid.UUID,
    delay_seconds: int,
    ctx: ToolContext,
    job_repo: JobRepository,
    audit_repo: AuditRepository,
) -> float:
    """Validate + push a single job onto the DLQ-replay ZSET.

    Same pre-check `JobService.replay_job` does (existence + state).
    Writes a `job.replay_scheduled` audit row so operators can see
    what was scheduled before it fires; the eventual promote loop
    writes the normal `job.replayed` row when it actually runs.
    """
    job = await job_repo.get_for_tenant(job_id, ctx.principal.tenant_id)
    if job is None:
        raise NotFoundError(f"Job {job_id} not found")
    if job.status not in (JobStatus.FAILED, JobStatus.DEAD_LETTER):
        from app.core.exceptions import JobError

        raise JobError(
            f"Only failed/dead_letter jobs can be replayed, got: {job.status}"
        )

    execute_at = await dlq_replay_scheduler.schedule_replay(
        ctx.redis,
        tenant_id=ctx.principal.tenant_id,
        principal_id=ctx.principal.id,
        job_id=job_id,
        delay_seconds=delay_seconds,
    )
    await audit_repo.log(
        "job.replay_scheduled",
        tenant_id=ctx.principal.tenant_id,
        user_id=(
            ctx.principal.user.id if ctx.principal.user is not None else None
        ),
        principal_type=ctx.principal.kind,
        principal_id=ctx.principal.id,
        job_id=job_id,
        resource_type="job",
        resource_id=str(job_id),
        request_id=request_id_var.get("") or None,
        extra_data={
            "delay_seconds": delay_seconds,
            "execute_at": execute_at,
        },
    )
    return execute_at
