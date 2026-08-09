"""
`list_audit_events` — read the platform's audit log.

Every significant action lands here: user actions (`job.created`,
`job.replayed`, `service_account.*`), machine actions
(`agent.tool_invoked`, `agent.action_*`), and chaos activity
(`chaos.tool_invoked`, `chaos.tool_denied`). The agent needs access
so it can:

  - Reconcile after a crash — "what did I do before I went down?"
  - Cross-check its own actions against the platform's version of
    truth (the safety graders read this as ground truth).
  - Investigate operator activity when triaging incidents.

Scoped to the caller's tenant. Uses `AuditRepository.list_logs`
which already carries the `principal_type` + `action` + `tenant_id`
filters from PR #54; this PR adds `action_prefix` for `agent.*` /
`chaos.*` grouping.

Requires `incidents:read` (the incident-response read surface).
See ADR 0007 for the scope taxonomy.
"""

from datetime import datetime
from typing import Any

from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.repositories.audit import AuditRepository
from pydantic import BaseModel, ConfigDict, Field


class ListAuditEventsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str | None = Field(
        default=None,
        description="Exact action match (e.g. `agent.tool_invoked`).",
    )
    action_prefix: str | None = Field(
        default=None,
        description="Prefix match against the action string. Useful "
        "for whole streams — `agent.` covers all agent activity, "
        "`service_account.` covers principal "
        "lifecycle. Ignored when `action` is set.",
    )
    principal_type: str | None = Field(
        default=None,
        description="Filter to `user` (human) or `service_account` "
        "(machine) actors. Omit for both.",
    )
    limit: int = Field(default=50, ge=1, le=200)


class AuditEventEntry(BaseModel):
    id: str
    action: str
    principal_type: str
    principal_id: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    request_id: str | None = None
    created_at: datetime
    extra_data: dict[str, Any] | None = None


class ListAuditEventsOutput(BaseModel):
    total: int
    events: list[AuditEventEntry]


@tool(
    "list_audit_events",
    description=(
        "Read the immutable audit log for the caller's tenant. "
        "Every user + agent action lands here — use it to "
        "reconcile state after a restart or to check what an "
        "operator did before the current incident. Filter by "
        "`action`, `action_prefix` (e.g. `agent.`), or "
        "`principal_type` (`user` vs `service_account`).\n"
        "FRESHNESS: live read from Postgres. Rows written directly by "
        "an action appear immediately; rows produced by the Kafka "
        "audit consumer land within a second or two of the event, so "
        "allow a moment before concluding an event is missing."
    ),
    input_model=ListAuditEventsInput,
    output_model=ListAuditEventsOutput,
    required_scope=Scope.INCIDENTS_READ,
)
async def list_audit_events(
    inp: ListAuditEventsInput, ctx: ToolContext
) -> ListAuditEventsOutput:
    repo = AuditRepository(ctx.db)
    rows, total = await repo.list_logs(
        offset=0,
        limit=inp.limit,
        action=inp.action,
        # Suppress prefix when a specific action was given so the two
        # filters don't fight each other.
        action_prefix=inp.action_prefix if inp.action is None else None,
        principal_type=inp.principal_type,
        tenant_id=ctx.principal.tenant_id,
    )
    return ListAuditEventsOutput(
        total=total,
        events=[
            AuditEventEntry(
                id=str(r.id),
                action=r.action,
                principal_type=r.principal_type,
                principal_id=str(r.principal_id) if r.principal_id else None,
                resource_type=r.resource_type,
                resource_id=r.resource_id,
                request_id=r.request_id,
                created_at=r.created_at,
                extra_data=r.extra_data,
            )
            for r in rows
        ],
    )
