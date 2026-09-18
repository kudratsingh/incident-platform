"""
`replay_dlq_by_ids` — targeted DLQ replay, immediate or scheduled.

Replays only the named ids, avoiding the blast radius of
`replay_dlq_messages(job_type=, limit=)`. Without `delay_seconds` it goes through
`JobService.replay_job` at once; with it, each job is pushed onto the
`jobs:dlq_replay_delayed` ZSET for the worker's promote loop — the `wait_and_replay`
category. Also the un-stick path for a chain whose root dead-lettered.
`actions:execute` + idempotent.
"""

import uuid

from app.core.exceptions import AppError, NotFoundError
from app.core.logging import get_logger
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.mcp.tools.actions._scheduled_replay import schedule_one_audited
from app.models.enums import JobStatus
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
    # Set only when `delay_seconds` was: the epoch second the promote loop fires at.
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
        "Re-check DLQ size only after `execute_at`.\n"
        "DAG ROOTS: a dead-lettered job that is a node in a dependency "
        "chain is replayed the same way, and this is the platform's "
        "un-stick path for a stalled chain. `dead_letter` is terminal "
        "and the resolver promotes a child only when every parent is "
        "`completed`, so replaying the root completes it and the held "
        "descendants promote. Verify with `get_dag_state(root_job_id)`. "
        "This is refused while any ancestor is paused (`pause_dag`) — "
        "the per-id result comes back `ok: false` — so do not pause a "
        "chain you intend to replay."
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
            # SAVEPOINT per item (#5): no earlier ids' writes behind an error.
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

        # Scheduled branch — pre-validate, then audit-then-arm in a savepoint.
        # Both excepts: AppError alone discarded audit rows for armed replays.
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
        except Exception as exc:
            failed += 1
            results.append(
                ReplayResult(id=str(job_id), ok=False, error=str(exc))
            )
            logger.exception(
                "replay_dlq_by_ids scheduled per-id crashed",
                extra={"job_id": str(job_id), "error": str(exc)},
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
    """Validate + arm a single job on the DLQ-replay ZSET.

    Same pre-check `JobService.replay_job` does. Ordering, savepoint and
    compensation live in `_scheduled_replay.schedule_one_audited`.
    """
    job = await job_repo.get_for_tenant(job_id, ctx.principal.tenant_id)
    if job is None:
        raise NotFoundError(f"Job {job_id} not found")
    if job.status not in (JobStatus.FAILED, JobStatus.DEAD_LETTER):
        from app.core.exceptions import JobError

        raise JobError(
            f"Only failed/dead_letter jobs can be replayed, got: {job.status}"
        )

    return await schedule_one_audited(
        ctx=ctx,
        audit_repo=audit_repo,
        job_id=job_id,
        delay_seconds=delay_seconds,
    )
