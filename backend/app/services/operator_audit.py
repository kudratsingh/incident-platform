"""
Operator audit — the immutable trail of every machine-principal action.

Every MCP tool call goes through `record_tool_invocation()`, so the row shape is uniform:
`action` is `agent.tool_invoked`, `extra_data` carries `tool_name`, `arguments`,
`scope_used`, `latency_ms`, `outcome` (Step 0 schema). `hidden_audit_action_prefixes` — the
read side of the same stream — lives here so writer and reader share one file.
"""

from typing import Any

from app.core.logging import get_logger
from app.core.scopes import Scope
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

# The stream prefix both share, spelled out and pinned to both by
# `tests/unit/test_operator_audit.py::test_chaos_actions_share_the_chaos_prefix`.
CHAOS_ACTION_PREFIX = "chaos."

# A third stream: the responder telling the platform what it is doing (ADR 0035). It
# sits under `agent.` because a service account really did make the call, but it is
# NOT `agent.tool_invoked` — that stream is what the agent did *to the platform*, and
# an operator timeline that mixed the two would colour a status report as an action.
AGENT_RUN_REPORTED_ACTION = "agent.run_reported"

# Outcome values recorded on tool invocation rows. Kept as a fixed set so
# admin filters and dashboards can rely on it.
OUTCOME_SUCCESS = "success"
OUTCOME_ERROR = "error"
OUTCOME_UNAUTHORIZED = "unauthorized"


def hidden_audit_action_prefixes(principal: Principal) -> tuple[str, ...]:
    """Audit-action prefixes `principal` must not be shown, ever.

    Two rules, and they point opposite ways on purpose. Callers pass the result to
    `list_logs`'s `exclude_action_prefixes`, so it lands in SQL and `total` counts only
    what the caller may see — a count is a fact about the rows.

    - The `chaos.` stream is shown **only** to a principal holding `chaos:invoke`: the
      lab is invisible to the agent under test (ADR 0012).
    - The `agent.run_reported` stream is hidden **from** a principal holding
      `agent_runs:write`: the writer of that stream is not its reader. The platform
      stores what a responder reports about itself and shows it to operators, never
      back to the responder (ADR 0035) — and without this the run reports would have
      come back through the audit tool, which is a read surface for `agent_runs` by
      another name.
    """
    hidden: list[str] = []
    if Scope.CHAOS_INVOKE.value not in principal.scopes:
        hidden.append(CHAOS_ACTION_PREFIX)
    if Scope.AGENT_RUNS_WRITE.value in principal.scopes:
        hidden.append(AGENT_RUN_REPORTED_ACTION)
    return tuple(hidden)


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
    is_commander: bool = False,
    denied_by: str | None = None,
) -> bool:
    """Write an `agent.tool_invoked` row for one MCP tool call; returns whether it staged.

    `is_chaos=True` uses `chaos.tool_invoked` (or `chaos.tool_denied` with `denied_by`) so
    chaos filters as its own stream (ADR 0008); `is_commander=True` uses
    `agent.run_reported` for the same reason (ADR 0035). The row shape is identical in all
    three cases, which is what lets the phase strip be rebuilt from the audit log alone:
    `extra_data.arguments` carries the run id and the state that was reported. Never
    raises — the savepoint costs only the audit row, and the caller decides:
    `app.mcp.handlers` treats `False` as fatal (R2-51).
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
    elif is_commander:
        action = AGENT_RUN_REPORTED_ACTION
    else:
        action = TOOL_INVOKED_ACTION

    try:
        # SAVEPOINT: a failing audit insert rolls back only the audit row, so the
        # tool's own writes and the response survive — the "never raises" contract.
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
        return False
    return True


__all__ = [
    "AGENT_RUN_REPORTED_ACTION",
    "CHAOS_ACTION_PREFIX",
    "CHAOS_TOOL_DENIED_ACTION",
    "CHAOS_TOOL_INVOKED_ACTION",
    "OUTCOME_ERROR",
    "OUTCOME_SUCCESS",
    "OUTCOME_UNAUTHORIZED",
    "TOOL_INVOKED_ACTION",
    "hidden_audit_action_prefixes",
    "record_tool_invocation",
]
