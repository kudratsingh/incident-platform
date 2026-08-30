"""
`get_trace` / `search_traces` — read the platform's trace history.

Traces are the correlation IDs carried on every job and every audit
row. `get_trace(trace_id)` pulls the job(s) + audit entries sharing
one trace so the agent has full context. `search_traces` runs a
constrained scan of recent jobs matching common filters and returns
matching trace IDs — cheap enough to run against the jobs index.

Both scoped to the caller's tenant. Both require `incidents:read`.

Both are bounded, and both say so (WO-R2-53). The agent cannot read this
docstring — the tool *description* is the whole interface — so a window
that behaves differently from what the description claims is a functional
defect. `get_trace` reports `truncated` plus the true totals rather than
promising completeness it cannot deliver; `search_traces` applies its
NULL-trace filter in SQL so untraced rows cannot eat the result budget.
"""

from datetime import datetime, timedelta
from typing import Any

from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.repositories.audit import AuditRepository
from app.repositories.job import JobRepository
from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# get_trace
# ---------------------------------------------------------------------------

# Result caps. Deliberate — a trace on a busy tenant can carry thousands of
# audit rows and an MCP response is read into a context window — but they used
# to be silent, under a description that promised "every artifact carrying a
# given trace_id" (WO-R2-53). A cap the caller cannot see is a cap that makes
# the caller wrong: an agent that reads 50 of 4000 jobs and concludes anything
# about the trace has been misled by the tool, not by the data. They are named
# here so the description, the output and the query cannot drift apart.
MAX_TRACE_JOBS = 50
MAX_TRACE_AUDIT_ROWS = 200


class GetTraceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(min_length=1, max_length=255)
    include_audit: bool = Field(
        default=True,
        description="Include audit rows sharing this trace_id.",
    )


class TracedJob(BaseModel):
    id: str
    type: str
    status: str
    user_id: str | None = None
    retry_count: int
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class TracedAuditRow(BaseModel):
    action: str
    resource_type: str | None = None
    resource_id: str | None = None
    principal_type: str
    created_at: datetime
    extra_data: dict[str, Any] | None = None


class GetTraceOutput(BaseModel):
    trace_id: str
    jobs: list[TracedJob]
    audit_events: list[TracedAuditRow]
    truncated: bool = Field(
        description="True when either list was cut short by the cap. When "
        "true, this response is a SAMPLE of the trace, not the trace — do "
        "not conclude anything about what it does not contain."
    )
    total_jobs: int = Field(
        description="How many jobs carry this trace_id in total, whether or "
        "not they were returned."
    )
    total_audit_events: int | None = Field(
        default=None,
        description="How many audit rows carry this trace_id in total. Null "
        "when `include_audit=false` — not counted rather than zero.",
    )


@tool(
    "get_trace",
    description=(
        "Fetch the artifacts carrying a given trace_id — job rows plus "
        "audit-log rows if `include_audit=true`. Useful for building a "
        "cold-start context around one incident: the trace is the "
        "correlation key that stitches the browser → API → worker → DB path.\n"
        f"CAPS: at most {MAX_TRACE_JOBS} jobs and {MAX_TRACE_AUDIT_ROWS} "
        "audit rows come back, newest first. `total_jobs` and "
        "`total_audit_events` give the real counts, and `truncated` is true "
        "when either list was cut short. A truncated response is a SAMPLE: "
        "do not read the absence of something from it. Narrow the trace or "
        "query the jobs directly if the totals are larger than the lists.\n"
        "FRESHNESS: live read from Postgres (jobs + audit rows), no "
        "cache and no dependency on the tracing backend — results are "
        "unaffected by whether an OTel collector is running."
    ),
    input_model=GetTraceInput,
    output_model=GetTraceOutput,
    required_scope=Scope.INCIDENTS_READ,
)
async def get_trace(inp: GetTraceInput, ctx: ToolContext) -> GetTraceOutput:
    job_repo = JobRepository(ctx.db)
    jobs, total_jobs = await job_repo.list_jobs(
        tenant_id=ctx.principal.tenant_id,
        offset=0,
        limit=MAX_TRACE_JOBS,
        trace_id=inp.trace_id,
    )

    audit_rows: list[TracedAuditRow] = []
    total_audit: int | None = None
    if inp.include_audit:
        audit_repo = AuditRepository(ctx.db)
        # Pull the audit rows carrying this request_id. AuditRepository
        # doesn't have a trace_id column — it stores the request_id which
        # is the same value in our middleware.
        #
        # Both predicates run in SQL: filtering request_id in Python over a
        # recent window made the lookup decay as audit_logs grew (every MCP
        # call appends a row), and the tenant filter is the actual isolation
        # here — audit_logs is in the RLS list but RLS is inert in the real
        # deployment. limit stays as a bound on the now-filtered query.
        rows, total_audit = await audit_repo.list_logs(
            offset=0,
            limit=MAX_TRACE_AUDIT_ROWS,
            request_id=inp.trace_id,
            tenant_id=ctx.principal.tenant_id,
        )
        for row in rows:
            audit_rows.append(
                TracedAuditRow(
                    action=row.action,
                    resource_type=row.resource_type,
                    resource_id=row.resource_id,
                    principal_type=row.principal_type,
                    created_at=row.created_at,
                    extra_data=row.extra_data,
                )
            )

    return GetTraceOutput(
        trace_id=inp.trace_id,
        truncated=total_jobs > len(jobs)
        or (total_audit is not None and total_audit > len(audit_rows)),
        total_jobs=total_jobs,
        total_audit_events=total_audit,
        jobs=[
            TracedJob(
                id=str(j.id),
                type=j.type,
                status=j.status,
                user_id=str(j.user_id) if j.user_id else None,
                retry_count=j.retry_count,
                error_message=j.error_message,
                created_at=j.created_at,
                updated_at=j.updated_at,
            )
            for j in jobs
        ],
        audit_events=audit_rows,
    )


# ---------------------------------------------------------------------------
# search_traces
# ---------------------------------------------------------------------------


class SearchTracesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str | None = None
    job_type: str | None = None
    since_hours: int | None = Field(
        default=None,
        ge=1,
        le=168,
        description="Only include jobs whose `created_at` is within the "
        "last N hours. Omit for no time bound.",
    )
    limit: int = Field(default=50, ge=1, le=200)


class TraceMatch(BaseModel):
    trace_id: str
    job_id: str
    job_type: str
    status: str
    created_at: datetime


class SearchTracesOutput(BaseModel):
    matches: list[TraceMatch]


@tool(
    "search_traces",
    description=(
        "Scan recent jobs by status / type / recency and return the "
        "matching trace_ids so the agent can drill into each with "
        "`get_trace`. Cheap — hits the jobs table with the usual "
        "indexes.\n"
        "SCOPE: only jobs that CARRY a trace_id are considered, and the "
        "filter is applied before the limit — untraced jobs never consume "
        "the budget. `limit` therefore bounds matches, not rows scanned. "
        "Newest first by submission time; there is no offset, so a limit "
        "hit means older matches exist that were not returned — narrow with "
        "`since_hours`, `status` or `job_type` rather than paging."
    ),
    input_model=SearchTracesInput,
    output_model=SearchTracesOutput,
    required_scope=Scope.INCIDENTS_READ,
)
async def search_traces(
    inp: SearchTracesInput, ctx: ToolContext
) -> SearchTracesOutput:
    from datetime import UTC

    created_after = None
    if inp.since_hours is not None:
        created_after = datetime.now(UTC) - timedelta(hours=inp.since_hours)

    job_repo = JobRepository(ctx.db)
    jobs, _ = await job_repo.list_jobs(
        tenant_id=ctx.principal.tenant_id,
        offset=0,
        limit=inp.limit,
        status=inp.status,
        job_type=inp.job_type,
        created_after=created_after,
        # The NULL-trace filter belongs in SQL, ahead of the LIMIT. Dropping
        # those rows in Python afterwards spent the budget on jobs that were
        # about to be discarded, so on a table dominated by untraced jobs the
        # tool reported "no traces" for traces that existed (WO-R2-53).
        require_trace_id=True,
    )

    matches = [
        TraceMatch(
            # Non-null by construction now — `require_trace_id` excludes both
            # NULL and empty. The fallback keeps the type checker happy
            # without pretending the column is non-nullable.
            trace_id=j.trace_id or "",
            job_id=str(j.id),
            job_type=j.type,
            status=j.status,
            created_at=j.created_at,
        )
        for j in jobs
    ]
    return SearchTracesOutput(matches=matches)
