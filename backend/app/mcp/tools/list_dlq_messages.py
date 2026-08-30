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
from app.repositories.job import JobRepository, JobSort
from app.repositories.triage import TriageRepository
from pydantic import BaseModel, ConfigDict, Field


class ListDlqMessagesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_type: str | None = Field(
        default=None,
        description="Filter to one job type (e.g. `csv_upload`, `report_gen`).",
    )
    remediation_hint: str | None = Field(
        default=None,
        description="Filter to entries the agent should treat one way. "
        "Values: `replay_safe`, `wait_and_replay`, `human_required`. "
        "Omit for all categories (including uncategorized).",
    )
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(
        default=0,
        ge=0,
        description="Rows to skip, for paging past the first `limit`. "
        "Compare against `total` to know whether more remain.",
    )


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
    remediation_hint: str | None = Field(
        default=None,
        description="Coarse category set by the platform's triage "
        "pipeline. See "
        "`RemediationHint` for the fixed value set. `null` means the "
        "platform hasn't categorized this entry yet — agent should "
        "treat as unknown, not as replay-safe.",
    )
    created_at: datetime = Field(
        description="When the job was SUBMITTED, not when it died. See "
        "`dead_lettered_at` for the latter — they can be days apart."
    )
    dead_lettered_at: datetime | None = Field(
        default=None,
        description="When the job reached its terminal state. This is the "
        "field the list is sorted by. Null only for rows that predate the "
        "column being stamped.",
    )
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
        "List jobs currently in the dead-letter state, most recently "
        "DEAD-LETTERED first — ordered by when each job died, not by when it "
        "was submitted, so a long-running job that failed a minute ago sorts "
        "above one submitted after it that failed hours earlier. "
        "Each entry carries `remediation_hint` (`replay_safe`, "
        "`wait_and_replay`, `human_required`, or null) so the agent can "
        "pick a strategy without re-reasoning about the error string. "
        "Filter by `remediation_hint=<value>` to isolate one category — "
        "e.g. pass `replay_safe` before calling `replay_dlq_by_category`. "
        "Also includes the LLM triage row when present.\n"
        "CATEGORY COVERAGE: `remediation_hint` is written by LLM triage, "
        "which is off by default on this platform. While it is off, jobs "
        "that dead-letter on their own arrive with a null hint and only "
        "entries categorised explicitly — e.g. by `mark_dlq_permanent` — "
        "carry one. So an all-null DLQ means nothing has classified "
        "these yet, NOT that they resist classification. A null hint is "
        "UNKNOWN, not replay-safe: do not feed those to a categorised "
        "replay. Read the error, then replay by explicit id, or fence it "
        "with `mark_dlq_permanent`.\n"
        "PAGING: `total` is the full count matching the filters, which can "
        "exceed the `limit` returned. Page with `offset` — `total` greater "
        "than `limit + offset` means there is more to fetch.\n"
        "FRESHNESS: live read from Postgres — reflects the moment of "
        "the call, no cache. Note that entries scheduled for a delayed "
        "replay still appear here until their `execute_at` passes, so "
        "an unchanged list right after a delayed replay is expected."
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
        offset=inp.offset,
        limit=inp.limit,
        status=JobStatus.DEAD_LETTER.value,
        job_type=inp.job_type,
        remediation_hint=inp.remediation_hint,
        # Dead-letter time, not submission time — see the tool description.
        # With no offset either, the newest dead-letters could sit past the
        # end of the only page the agent was able to fetch (WO-R2-53).
        sort=JobSort.DEAD_LETTERED_AT,
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
                remediation_hint=job.remediation_hint,
                created_at=job.created_at,
                dead_lettered_at=job.completed_at,
                updated_at=job.updated_at,
                trace_id=job.trace_id,
                triage=triage_summary,
            )
        )

    return ListDlqMessagesOutput(total=total, items=items)
