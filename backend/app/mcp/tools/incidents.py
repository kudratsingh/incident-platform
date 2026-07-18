"""
`get_incident` / `list_incidents` — read the alert stream.

Incidents in this platform are `alerts` rows (see Wave 1 PR B). The
naming split follows the vocabulary the agent thinks in: "list the
current incidents", "give me details on incident X". The paired
`list_active_alerts` tool is a strict subset (unresolved only, no id
lookup); these are the general-purpose read surface.

Both scoped to caller's tenant. Both `incidents:read`.
"""

import uuid
from datetime import datetime
from typing import Any

from app.core.exceptions import NotFoundError
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.models.alert import Alert
from app.repositories.alert import AlertRepository
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, desc, func, select

# ---------------------------------------------------------------------------
# list_incidents
# ---------------------------------------------------------------------------


class ListIncidentsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_resolved: bool = Field(
        default=False,
        description="When true, include incidents that have been resolved. "
        "Useful for reviewing history; the default matches how "
        "`list_active_alerts` behaves.",
    )
    severity: str | None = Field(
        default=None,
        description="Filter to `info`, `warning`, or `critical`.",
    )
    source: str | None = Field(
        default=None,
        description="Filter by source string (e.g. `slo:completion`).",
    )
    limit: int = Field(default=50, ge=1, le=200)


class IncidentSummary(BaseModel):
    id: str
    severity: str
    source: str
    title: str
    description: str | None = None
    fired_at: datetime
    resolved_at: datetime | None = None
    is_active: bool


class ListIncidentsOutput(BaseModel):
    total: int
    incidents: list[IncidentSummary]


async def _list_incidents_query(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    include_resolved: bool,
    severity: str | None,
    source: str | None,
    limit: int,
) -> tuple[list[Alert], int]:
    filters = [Alert.tenant_id == tenant_id]
    if not include_resolved:
        filters.append(Alert.resolved_at.is_(None))
    if severity is not None:
        filters.append(Alert.severity == severity)
    if source is not None:
        filters.append(Alert.source == source)

    where = and_(*filters)
    total_result = await session.execute(
        select(func.count()).select_from(Alert).where(where)
    )
    total = int(total_result.scalar_one())

    rows_result = await session.execute(
        select(Alert).where(where).order_by(desc(Alert.fired_at)).limit(limit)
    )
    return list(rows_result.scalars().all()), total


@tool(
    "list_incidents",
    description=(
        "List incidents (alerts) scoped to the caller's tenant. Defaults "
        "to unresolved; pass `include_resolved=true` to see history. "
        "Superset of `list_active_alerts` — that tool is the cheap "
        "poll path when you know you want unresolved only."
    ),
    input_model=ListIncidentsInput,
    output_model=ListIncidentsOutput,
    required_scope=Scope.INCIDENTS_READ,
)
async def list_incidents(
    inp: ListIncidentsInput, ctx: ToolContext
) -> ListIncidentsOutput:
    rows, total = await _list_incidents_query(
        ctx.db,
        tenant_id=ctx.principal.tenant_id,
        include_resolved=inp.include_resolved,
        severity=inp.severity,
        source=inp.source,
        limit=inp.limit,
    )
    return ListIncidentsOutput(
        total=total,
        incidents=[
            IncidentSummary(
                id=str(a.id),
                severity=a.severity,
                source=a.source,
                title=a.title,
                description=a.description,
                fired_at=a.fired_at,
                resolved_at=a.resolved_at,
                is_active=a.resolved_at is None,
            )
            for a in rows
        ],
    )


# ---------------------------------------------------------------------------
# get_incident
# ---------------------------------------------------------------------------


class GetIncidentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID


class GetIncidentOutput(BaseModel):
    id: str
    severity: str
    source: str
    title: str
    description: str | None = None
    fired_at: datetime
    resolved_at: datetime | None = None
    is_active: bool
    extra_data: dict[str, Any] | None = None
    request_id: str | None = None


@tool(
    "get_incident",
    description=(
        "Fetch full details for one incident by id, including the "
        "`extra_data` blob the emitting source attached."
    ),
    input_model=GetIncidentInput,
    output_model=GetIncidentOutput,
    required_scope=Scope.INCIDENTS_READ,
)
async def get_incident(
    inp: GetIncidentInput, ctx: ToolContext
) -> GetIncidentOutput:
    alert = await AlertRepository(ctx.db).get_by_id(inp.id)
    if alert is None or alert.tenant_id != ctx.principal.tenant_id:
        # Same-shape error whether the row genuinely doesn't exist or
        # sits in a sibling tenant — never confirm cross-tenant IDs.
        raise NotFoundError(f"incident not found: {inp.id}")

    return GetIncidentOutput(
        id=str(alert.id),
        severity=alert.severity,
        source=alert.source,
        title=alert.title,
        description=alert.description,
        fired_at=alert.fired_at,
        resolved_at=alert.resolved_at,
        is_active=alert.resolved_at is None,
        extra_data=alert.extra_data,
        request_id=alert.request_id,
    )
