"""
`mark_dlq_permanent` — fence a DLQ entry as human_required.

For entries the agent decides are not safe to auto-replay (persistent
bug in the job or the underlying data). Pulls the entry out of every
future `replay_dlq_by_category(replay_safe)` /
`replay_dlq_by_category(wait_and_replay)` scan by setting
`remediation_hint=human_required` on the job, and stamps `fenced_at` /
`fenced_by` so the fence is visible on the row itself.

The audit row carries the operator's `reason` string so downstream
review has context.

Doesn't change the job's `status` — it stays `dead_letter`. The
signal is the hint field, not a state transition.

## Every mark writes (WO-R2-158)

This tool used to do nothing at all when the row was already
`human_required`: it read `already_marked`, skipped the update, skipped
the audit row, and returned. Two things were wrong with that.

  * **A fence was not observable.** `human_required` is the same value
    whether LLM triage classified the row or an operator fenced it, so
    nothing on the row said a fence had happened. `fenced_at` and
    `fenced_by` are now that statement, and `fenced_at` is the field a
    caller verifies a fence on — re-reading the hint proves only that
    *somebody* classified the row at some point.
  * **An idempotent re-fence left no trace.** Confirmed live 2026-09-08.
    A row seeded `human_required` made a fence a no-op, so an agent that
    fenced it and an agent that skipped the step left identical worlds and
    an eval could not tell them apart. Re-fencing is still an operator
    action: it is a deliberate decision about this row, taken now, with a
    reason worth keeping. So every mark writes `fenced_at`, `fenced_by`
    and an audit row, whatever the hint was before.

`already_marked` stays in the response for callers that care whether they
were first — it no longer suppresses the write, and the audit row carries
the same flag.

`is_idempotent=True` is unchanged and means what it always meant: a repeat
of the same `idempotency_key` returns the stored response without
re-executing (ADR 0010). "Every mark writes" is per execution. A caller
that genuinely wants to re-fence — a second decision about the same row —
passes a new key, exactly as it would to re-run any Tier-1 action.

`actions:execute` + idempotent.
"""

import uuid
from datetime import UTC, datetime

from app.core.exceptions import NotFoundError
from app.core.logging import get_logger, request_id_var
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.models.enums import JobStatus, RemediationHint
from app.repositories.audit import AuditRepository
from app.repositories.job import JobRepository
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)


class MarkDlqPermanentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID = Field(
        description="DLQ job to mark. Must be a real job in the caller's "
        "tenant with status=dead_letter."
    )
    reason: str = Field(
        min_length=8,
        max_length=1024,
        description="Why the entry is not replayable. Recorded on the "
        "audit row for the human review path — write a full sentence.",
    )
    idempotency_key: str = Field(min_length=8, max_length=255)


class MarkDlqPermanentOutput(BaseModel):
    job_id: str
    previous_hint: str | None
    remediation_hint: str
    already_marked: bool = Field(
        description="True when the row already carried `human_required` "
        "before this call. The fence was still applied and still "
        "audited — this only says the caller was not the first to "
        "classify the row."
    )
    fenced_at: datetime = Field(
        description="When this call fenced the row — the platform's own "
        "clock, UTC. Set on every mark, so it moves on a re-mark. Also "
        "readable on the row via `list_dlq_messages`, which is how a "
        "caller verifies the fence landed."
    )


@tool(
    "mark_dlq_permanent",
    description=(
        "Fence a DLQ entry as `human_required` — pulls it out of future "
        "`replay_dlq_by_category` scans and writes the operator's reason "
        "to the audit trail. Use when analysis shows a persistent bug "
        "that auto-replay would just re-hit. Doesn't change job.status "
        "— the entry stays in DLQ, just won't be auto-replayed.\n"
        "VERIFYING THE FENCE: `fenced_at` is the surface. Every mark "
        "stamps it (platform clock, UTC) together with `fenced_by`, and "
        "both come back on the row from `list_dlq_messages`. Re-reading "
        "`remediation_hint` is NOT verification: `human_required` is the "
        "same value whether triage classified the row or somebody fenced "
        "it, so the hint cannot tell you your own call landed.\n"
        "RE-MARKING AN ALREADY-FENCED ROW: it is not a no-op. The hint is "
        "unchanged, but `fenced_at` and `fenced_by` are re-stamped and an "
        "audit row is written, because deciding again that this row needs "
        "a person is still an operator action. The response reports "
        "`already_marked: true` so a caller can tell it was not the "
        "first. Idempotent in the usual sense: repeating the same "
        "`idempotency_key` returns the stored response without "
        "re-executing, so a deliberate re-fence needs a new key."
    ),
    input_model=MarkDlqPermanentInput,
    output_model=MarkDlqPermanentOutput,
    required_scope=Scope.ACTIONS_EXECUTE,
    is_idempotent=True,
)
async def mark_dlq_permanent(
    inp: MarkDlqPermanentInput, ctx: ToolContext
) -> MarkDlqPermanentOutput:
    job_repo = JobRepository(ctx.db)
    audit_repo = AuditRepository(ctx.db)

    job = await job_repo.get_by_id(inp.job_id)
    if job is None or job.tenant_id != ctx.principal.tenant_id:
        # Same-shape not-found whether the row is missing or in a
        # sibling tenant — never confirm cross-tenant IDs.
        raise NotFoundError(f"job not found: {inp.job_id}")
    if job.status != JobStatus.DEAD_LETTER.value:
        raise NotFoundError(
            f"job {inp.job_id} is not in dead_letter (status={job.status})"
        )

    previous_hint = job.remediation_hint
    already_marked = previous_hint == RemediationHint.HUMAN_REQUIRED.value
    principal_type = (
        "service_account"
        if ctx.principal.kind == "service_account"
        else "user"
    )
    # Aware UTC, like every other app-set timestamp on this table — the
    # columns are TIMESTAMP WITH TIME ZONE and downstream Python math on
    # them uses aware datetimes (`JobRepository.update_status`).
    fenced_at = datetime.now(UTC)

    # Unconditional. `already_marked` describes what the row looked like
    # before; it does not decide whether this call writes. See the module
    # docstring — the early return this replaces is what made a fence on an
    # already-classified row leave no trace anywhere.
    job.remediation_hint = RemediationHint.HUMAN_REQUIRED.value
    job.fenced_at = fenced_at
    # `"{principal_type}:{principal_id}"` — self-describing because the id
    # alone cannot say which table it belongs to (ADR 0007).
    job.fenced_by = f"{principal_type}:{ctx.principal.id}"
    await job_repo.session.flush()
    await audit_repo.log(
        "job.marked_permanent",
        tenant_id=job.tenant_id,
        job_id=job.id,
        principal_type=principal_type,
        principal_id=ctx.principal.id,
        resource_type="job",
        resource_id=str(job.id),
        request_id=request_id_var.get("") or None,
        extra_data={
            "reason": inp.reason,
            "previous_hint": previous_hint,
            "new_hint": RemediationHint.HUMAN_REQUIRED.value,
            # Whether this was the first fence or a re-fence. On the row
            # only the latest fence survives; the audit trail is where the
            # sequence lives, so it has to say which kind each one was.
            "already_marked": already_marked,
            "fenced_at": fenced_at.isoformat(),
        },
    )
    logger.warning(
        "dlq entry marked permanent",
        extra={
            "job_id": str(job.id),
            "reason": inp.reason,
            "previous_hint": previous_hint,
            "already_marked": already_marked,
        },
    )

    return MarkDlqPermanentOutput(
        job_id=str(job.id),
        previous_hint=previous_hint,
        remediation_hint=RemediationHint.HUMAN_REQUIRED.value,
        already_marked=already_marked,
        fenced_at=fenced_at,
    )
