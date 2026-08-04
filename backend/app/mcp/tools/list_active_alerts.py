"""
`list_active_alerts` — poll-based read of unresolved alerts.

The paired push channel is the HMAC-signed webhook fired from
`AlertService.create_alert` (see `app/services/alerts.py`). Agents
that can't accept inbound webhooks (or want a catch-up path after a
missed delivery) poll this tool.

Scoped to the caller's tenant via the existing `Principal.tenant_id`
context. Postgres RLS additionally enforces it at the DB layer — this
tool is defense-in-depth on top of that.

Requires `incidents:read`.
"""

from datetime import datetime
from typing import Any

from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.repositories.alert import AlertRepository
from pydantic import BaseModel, ConfigDict, Field


class ListActiveAlertsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: str | None = Field(
        default=None,
        description="Filter by severity: `info`, `warning`, `critical`. "
        "Omit for all.",
    )
    limit: int = Field(default=50, ge=1, le=200)


class AlertSummary(BaseModel):
    id: str
    severity: str
    source: str
    title: str
    description: str | None = None
    fired_at: datetime
    extra_data: dict[str, Any] | None = None


class ListActiveAlertsOutput(BaseModel):
    total: int
    alerts: list[AlertSummary]


@tool(
    "list_active_alerts",
    description=(
        "List unresolved alerts scoped to the caller's tenant. Alerts "
        "come from platform sources (SLO breach, DLQ threshold, chaos "
        "runs, etc.). Prefer receiving the HMAC-signed webhook if you "
        "can — this endpoint is a poll fallback.\n"
        "FRESHNESS: live read from Postgres — reflects the moment of "
        "the call, no cache."
    ),
    input_model=ListActiveAlertsInput,
    output_model=ListActiveAlertsOutput,
    required_scope=Scope.INCIDENTS_READ,
)
async def list_active_alerts(
    inp: ListActiveAlertsInput, ctx: ToolContext
) -> ListActiveAlertsOutput:
    repo = AlertRepository(ctx.db)
    rows, total = await repo.list_active_for_tenant(
        tenant_id=ctx.principal.tenant_id,
        offset=0,
        limit=inp.limit,
        severity=inp.severity,
    )
    return ListActiveAlertsOutput(
        total=total,
        alerts=[
            AlertSummary(
                id=str(a.id),
                severity=a.severity,
                source=a.source,
                title=a.title,
                description=a.description,
                fired_at=a.fired_at,
                extra_data=a.extra_data,
            )
            for a in rows
        ],
    )
