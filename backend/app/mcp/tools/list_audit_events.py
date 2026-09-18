"""
`list_audit_events` — read the platform's audit log, tenant-scoped.

Every user and machine action lands here, so the agent can reconcile after a crash
and cross-check its own (the safety graders read this as ground truth).
`hidden_audit_action_prefixes` in `app.services.operator_audit` withholds the lab's
stream, in SQL and out of `total`, from a principal that cannot fire it (ADR 0012).
Requires `incidents:read`.
"""

from datetime import datetime
from typing import Any, Literal

from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.repositories.audit import AuditRepository
from app.services.operator_audit import hidden_audit_action_prefixes
from pydantic import BaseModel, ConfigDict, Field

# The two principal shapes that write to `audit_logs`, mirroring
# `app.models.audit.PRINCIPAL_TYPE_USER` / `PRINCIPAL_TYPE_SERVICE_ACCOUNT`.
# Spelled out, not derived: these strings are baked verbatim into this tool's
# inputSchema, and `test_principal_type_literal_matches_the_model_constants` fails
# if the pair drifts. The column itself stays a lax string.
_PRINCIPAL_TYPES = Literal["user", "service_account"]


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
    principal_type: _PRINCIPAL_TYPES | None = Field(
        default=None,
        description="Filter to `user` (human) or `service_account` "
        "(machine) actors. Omit for both. An unrecognised value is "
        "rejected at parse time as an invalid-params error rather than "
        "matching no rows — an empty result reads as 'nothing happened' "
        "when the truth is 'you asked the wrong question'.",
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
        "allow a moment before concluding an event is missing.\n"
        "PAGINATION: newest first, ordered by `created_at` (the time "
        "the audit row was written, not the time the action was "
        "requested — for consumer-written rows those differ by the "
        "delivery lag above), tiebroken by `id`. There is NO offset: "
        "you always get the newest `limit` rows matching the filters "
        "and cannot page past them. `total` is the full count of "
        "matching rows and may exceed the number returned — when it "
        "does, older matches exist that this call did not show you, "
        "and the only way to reach them is a narrower filter."
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
        # Streams this principal may not read, AND-ed with what it asked for. A
        # filter on a withheld stream gets `total: 0`, not a revealing refusal.
        exclude_action_prefixes=hidden_audit_action_prefixes(ctx.principal),
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
