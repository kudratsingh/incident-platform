"""
Operator audit — the immutable trail of every machine-principal action.

Every MCP tool call goes through `record_tool_invocation()` (or one of the
sibling helpers) so the audit row shape is uniform: `action` is always
`agent.tool_invoked`, `extra_data` carries `tool_name`, `arguments`,
`scope_used`, `latency_ms`, `outcome` — the schema locked in Step 0.

The helpers accept a `Principal` from `app.dependencies` so the caller
doesn't have to plumb principal_type / principal_id manually. Human
callers can also invoke `record_tool_invocation` (nothing enforces
service-account-only at this layer), but in practice only MCP handlers
will — humans go through the regular admin API paths whose audit shape
is different.
"""

from typing import Any

from app.core.logging import get_logger
from app.dependencies import Principal
from app.models.audit import (
    PRINCIPAL_TYPE_SERVICE_ACCOUNT,
    PRINCIPAL_TYPE_USER,
)
from app.repositories.audit import AuditRepository

logger = get_logger(__name__)

TOOL_INVOKED_ACTION = "agent.tool_invoked"
# Chaos tool activity is a separate audit stream so operators can filter
# it independently — see ADR 0008 + CLAUDE.md agent-facing surface.
CHAOS_TOOL_INVOKED_ACTION = "chaos.tool_invoked"
CHAOS_TOOL_DENIED_ACTION = "chaos.tool_denied"

# Outcome values recorded on tool invocation rows. Kept as a fixed set so
# admin filters and dashboards can rely on it.
OUTCOME_SUCCESS = "success"
OUTCOME_ERROR = "error"
OUTCOME_UNAUTHORIZED = "unauthorized"


def _principal_kwargs(principal: Principal) -> dict[str, Any]:
    """Translate a Principal into the audit-row identity fields."""
    if principal.kind == "service_account":
        return {
            "principal_type": PRINCIPAL_TYPE_SERVICE_ACCOUNT,
            "principal_id": principal.id,
            "user_id": None,
        }
    return {
        "principal_type": PRINCIPAL_TYPE_USER,
        "principal_id": principal.id,
        "user_id": principal.id,
    }


async def record_tool_invocation(
    audit_repo: AuditRepository,
    *,
    principal: Principal,
    tool_name: str,
    arguments: dict[str, Any] | None,
    scope_used: str | None,
    latency_ms: float,
    outcome: str,
    error_message: str | None = None,
    request_id: str | None = None,
    is_chaos: bool = False,
    denied_by: str | None = None,
) -> None:
    """Write an `agent.tool_invoked` row for a single MCP tool call.

    When `is_chaos=True` the action is `chaos.tool_invoked` (or
    `chaos.tool_denied` if `denied_by` is set), so chaos activity
    filters cleanly on the admin Audit tab as a separate stream from
    general agent traffic — see ADR 0008.

    Never raises — audit failures must not turn a successful tool call
    into a client-visible error. If persistence fails the row is dropped
    and a structured log line is written by the caller's session commit.
    """
    extra: dict[str, Any] = {
        "tool_name": tool_name,
        "arguments": arguments or {},
        "scope_used": scope_used,
        "latency_ms": round(latency_ms, 3),
        "outcome": outcome,
    }
    if error_message is not None:
        extra["error_message"] = error_message
    if denied_by is not None:
        extra["denied_by"] = denied_by

    if is_chaos:
        action = (
            CHAOS_TOOL_DENIED_ACTION if denied_by is not None else CHAOS_TOOL_INVOKED_ACTION
        )
    else:
        action = TOOL_INVOKED_ACTION

    try:
        # SAVEPOINT: a failing audit insert (FK drift, constraint bug —
        # the replay_job/#70 class) rolls back only the audit row. The
        # tool's own writes and the caller's response are unaffected,
        # which is what the "never raises" contract above requires.
        async with audit_repo.session.begin_nested():
            await audit_repo.log(
                action,
                tenant_id=principal.tenant_id,
                resource_type="mcp_tool",
                resource_id=tool_name,
                request_id=request_id,
                extra_data=extra,
                **_principal_kwargs(principal),
            )
    except Exception:
        logger.exception(
            "audit write failed; dropping tool-invocation row",
            extra={"tool_name": tool_name, "outcome": outcome},
        )


__all__ = [
    "CHAOS_TOOL_DENIED_ACTION",
    "CHAOS_TOOL_INVOKED_ACTION",
    "OUTCOME_ERROR",
    "OUTCOME_SUCCESS",
    "OUTCOME_UNAUTHORIZED",
    "TOOL_INVOKED_ACTION",
    "record_tool_invocation",
]
