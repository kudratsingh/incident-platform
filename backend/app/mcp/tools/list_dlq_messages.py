"""
`list_dlq_messages` — jobs the platform gave up on.

Scoped to the caller's tenant. Reads via the existing `JobRepository`
so any RLS + tenant-scope invariants applied at the SQL layer already
guard this path. Requires `incidents:read`.

Fields emitted per row are the ones the agent's LLM needs to hypothesize
about a failure: what job type, what error message, how many retries,
when it died. The optional `triage` block includes the Phase-10 LLM
triage row when present so the agent can build on the platform's own
first-pass analysis instead of duplicating it.
"""

from datetime import datetime
from typing import Any

from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.models.enums import JobStatus
from app.repositories.job import JobRepository
from app.repositories.triage import TriageRepository
from pydantic import BaseModel, ConfigDict, Field


class ListDlqMessagesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_type: str | None = Field(
        default=None,
        description="Filter to one job type (e.g. `csv_upload`, `report_gen`).",
    )
    limit: int = Field(default=50, ge=1, le=200)


class DlqTriageSummary(BaseModel):
    root_cause_category: str | None = None
    summary: str
    suggested_fix: str | None = None
    is_retryable: bool | None = None
    confidence: float | None = None


class DlqEntry(BaseModel):
    id: str
    type: str
    error_message: str | None = None
    retry_count: int
    created_at: datetime
    updated_at: datetime | None = None
    trace_id: str | None = None
    triage: DlqTriageSummary | None = None
    extra: dict[str, Any] | None = None


class ListDlqMessagesOutput(BaseModel):
    total: int
    items: list[DlqEntry]


@tool(
    "list_dlq_messages",
    description=(
        "List jobs currently in the dead-letter state, most recent first. "
        "Includes the LLM triage row for each job when the platform has "
        "one, so the agent can build on the first-pass analysis instead of "
        "starting from raw error strings."
    ),
    input_model=ListDlqMessagesInput,
    output_model=ListDlqMessagesOutput,
    required_scope=Scope.INCIDENTS_READ,
)
async def list_dlq_messages(
    inp: ListDlqMessagesInput, ctx: ToolContext
) -> ListDlqMessagesOutput:
    job_repo = JobRepository(ctx.db)
    triage_repo = TriageRepository(ctx.db)

    jobs, total = await job_repo.list_jobs(
        tenant_id=ctx.principal.tenant_id,
        offset=0,
        limit=inp.limit,
        status=JobStatus.DEAD_LETTER.value,
        job_type=inp.job_type,
    )

    items: list[DlqEntry] = []
    for job in jobs:
        triage = await triage_repo.get_by_job_id(job.id)
        triage_summary: DlqTriageSummary | None = None
        if triage is not None:
            triage_summary = DlqTriageSummary(
                root_cause_category=getattr(triage, "root_cause_category", None),
                summary=triage.summary,
                suggested_fix=getattr(triage, "suggested_fix", None),
                is_retryable=getattr(triage, "is_retryable", None),
                confidence=getattr(triage, "confidence", None),
            )
        items.append(
            DlqEntry(
                id=str(job.id),
                type=job.type,
                error_message=job.error_message,
                retry_count=job.retry_count,
                created_at=job.created_at,
                updated_at=job.updated_at,
                trace_id=job.trace_id,
                triage=triage_summary,
            )
        )

    return ListDlqMessagesOutput(total=total, items=items)
