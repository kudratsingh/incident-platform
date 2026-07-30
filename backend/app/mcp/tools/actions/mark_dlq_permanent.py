"""
`mark_dlq_permanent` — flag a DLQ entry as human_required.

For entries the agent decides are not safe to auto-replay (persistent
bug in the job or the underlying data). Pulls the entry out of every
future `replay_dlq_by_category(replay_safe)` /
`replay_dlq_by_category(wait_and_replay)` scan by setting
`remediation_hint=human_required` on the job.

The audit row carries the operator's `reason` string so downstream
review has context.

Doesn't change the job's `status` — it stays `dead_letter`. The
signal is the hint field, not a state transition.

`actions:execute` + idempotent.
"""

import uuid

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
    already_marked: bool


@tool(
    "mark_dlq_permanent",
    description=(
        "Flag a DLQ entry as `human_required` — pulls it out of "
        "future `replay_dlq_by_category` scans and writes the "
        "operator's reason to the audit trail. Use when analysis "
        "shows a persistent bug that auto-replay would just re-hit. "
        "Doesn't change job.status — the entry stays in DLQ, just "
        "won't be auto-replayed. Idempotent."
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

    if not already_marked:
        job.remediation_hint = RemediationHint.HUMAN_REQUIRED.value
        await job_repo.session.flush()
        await audit_repo.log(
            "job.marked_permanent",
            tenant_id=job.tenant_id,
            job_id=job.id,
            principal_type="service_account"
            if ctx.principal.kind == "service_account"
            else "user",
            principal_id=ctx.principal.id,
            resource_type="job",
            resource_id=str(job.id),
            request_id=request_id_var.get("") or None,
            extra_data={
                "reason": inp.reason,
                "previous_hint": previous_hint,
                "new_hint": RemediationHint.HUMAN_REQUIRED.value,
            },
        )
        logger.warning(
            "dlq entry marked permanent",
            extra={
                "job_id": str(job.id),
                "reason": inp.reason,
                "previous_hint": previous_hint,
            },
        )

    return MarkDlqPermanentOutput(
        job_id=str(job.id),
        previous_hint=previous_hint,
        remediation_hint=RemediationHint.HUMAN_REQUIRED.value,
        already_marked=already_marked,
    )
