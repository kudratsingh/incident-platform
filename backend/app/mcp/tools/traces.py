"""
`get_trace` / `search_traces` — read the platform's trace history.

Traces are the correlation IDs carried on every job and every audit
row. `get_trace(trace_id)` pulls the job(s) + audit entries sharing
one trace so the agent has full context. `search_traces` runs a
constrained scan of recent jobs matching common filters and returns
matching trace IDs — cheap enough to run against the jobs index.

Both scoped to the caller's tenant. Both require `incidents:read`.
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


@tool(
    "get_trace",
    description=(
        "Fetch every artifact carrying a given trace_id — job rows plus "
        "audit-log rows if `include_audit=true`. Useful for building a "
        "cold-start context around one incident: the trace is the "
        "correlation key that stitches the browser → API → worker → DB path.\n"
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
    jobs, _ = await job_repo.list_jobs(
        tenant_id=ctx.principal.tenant_id,
        offset=0,
        limit=50,
        trace_id=inp.trace_id,
    )

    audit_rows: list[TracedAuditRow] = []
    if inp.include_audit:
        audit_repo = AuditRepository(ctx.db)
        # Pull recent audit rows carrying this request_id. AuditRepository
        # doesn't have a trace_id column — it stores the request_id which
        # is the same value in our middleware.
        rows, _ = await audit_repo.list_logs(
            offset=0,
            limit=200,
            principal_type=None,
        )
        for row in rows:
            if row.request_id == inp.trace_id:
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
        "indexes."
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
    )

    matches = [
        TraceMatch(
            trace_id=j.trace_id or "",
            job_id=str(j.id),
            job_type=j.type,
            status=j.status,
            created_at=j.created_at,
        )
        for j in jobs
        if j.trace_id
    ]
    return SearchTracesOutput(matches=matches)
