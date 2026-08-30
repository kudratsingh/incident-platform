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

`delay_seconds` (1..3600) defers the enqueue for each matched job —
push into the `jobs:dlq_replay_delayed` ZSET and a worker loop
fires the replay when the delay elapses. Intended pairing with
`wait_and_replay`: give the transient dependency time to recover
before retrying en masse.

`actions:execute` + idempotent. Bounded by `max_replays` (default
20, capped at 100).
"""

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.mcp.tools.actions._scheduled_replay import schedule_one_audited
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
    delay_seconds: int | None = Field(
        default=None,
        ge=1,
        le=3600,
        description=(
            "If set, the platform defers each replay by this many "
            "seconds. Use with `wait_and_replay` when the underlying "
            "dependency needs a moment to recover. Omit for immediate "
            "replay. Cap of 1 hour."
        ),
    )
    idempotency_key: str = Field(min_length=8, max_length=255)


class ReplayDlqByCategoryOutput(BaseModel):
    category: str
    matched: int
    replayed: int = 0
    scheduled: int = 0
    failed: int
    job_ids: list[str]
    # Populated when `delay_seconds` was set — the epoch second the
    # last-scheduled replay fires at. Cheap "when do I check back" hint.
    execute_at: float | None = None


@tool(
    "replay_dlq_by_category",
    description=(
        "Bulk-replay every DLQ entry in one remediation category "
        "(`replay_safe` or `wait_and_replay`). Refuses "
        "`human_required` — those must go through a human review "
        "path. Bounded by `max_replays` (default 20). Idempotent.\n"
        "VERIFYING A DELAYED REPLAY: when `delay_seconds` is set, "
        "nothing is enqueued yet. The matched entries stay in status "
        "`dead_letter` and remain visible in list_dlq_messages until "
        "`execute_at` passes — a DLQ that has not shrunk is the "
        "expected state, not a failed replay. Verify with the "
        "`scheduled` count in this response and the "
        "`job.replay_scheduled` audit events, not with DLQ size. "
        "Re-check DLQ size only after `execute_at`."
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

    audit_repo = AuditRepository(ctx.db)
    service = JobService(
        job_repo,
        audit_repo,
        OutboxRepository(ctx.db),
        ctx.redis,
        dep_repo=JobDependencyRepository(ctx.db),
    )
    replayed_ids: list[str] = []
    scheduled_ids: list[str] = []
    failed = 0
    last_execute_at: float | None = None

    for job in jobs:
        if inp.delay_seconds is None:
            # SAVEPOINT per item (#5) — same rationale as
            # replay_dlq_messages: bound a mid-loop non-AppError so it
            # can't commit a partial batch behind an error response.
            try:
                async with ctx.db.begin_nested():
                    await service.replay_job(
                        job_id=job.id,
                        tenant_id=ctx.principal.tenant_id,
                        principal_type=ctx.principal.kind,
                        principal_id=ctx.principal.id,
                    )
                replayed_ids.append(str(job.id))
            except AppError as exc:
                failed += 1
                logger.warning(
                    "replay_dlq_by_category per-job failure",
                    extra={"job_id": str(job.id), "error": exc.message},
                )
            except Exception as exc:
                failed += 1
                logger.exception(
                    "replay_dlq_by_category per-job crashed",
                    extra={"job_id": str(job.id), "error": str(exc)},
                )
            continue

        # Scheduled branch — audit-then-arm inside a savepoint, shared
        # with `replay_dlq_by_ids` (`_scheduled_replay`). Both excepts
        # are load-bearing: catching only AppError, as this branch used
        # to, let any other mid-loop error abort the whole tool and
        # discard the audit rows for replays already armed.
        try:
            execute_at = await schedule_one_audited(
                ctx=ctx,
                audit_repo=audit_repo,
                job_id=job.id,
                delay_seconds=inp.delay_seconds,
                extra_data={"category": inp.category},
            )
            scheduled_ids.append(str(job.id))
            last_execute_at = execute_at
        except AppError as exc:
            failed += 1
            logger.warning(
                "replay_dlq_by_category scheduled per-job failure",
                extra={"job_id": str(job.id), "error": exc.message},
            )
        except Exception as exc:
            failed += 1
            logger.exception(
                "replay_dlq_by_category scheduled per-job crashed",
                extra={"job_id": str(job.id), "error": str(exc)},
            )

    return ReplayDlqByCategoryOutput(
        category=inp.category,
        matched=len(jobs),
        replayed=len(replayed_ids),
        scheduled=len(scheduled_ids),
        failed=failed,
        job_ids=replayed_ids + scheduled_ids,
        execute_at=last_execute_at,
    )


class ReplayDlqCategoryRefusedError(AppError):
    status_code = 400
    error_code = "dlq_category_refused"
