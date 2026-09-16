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

This module also owns the *read* side of that stream's visibility rule:
`hidden_audit_action_prefixes` below, which the MCP read tools apply so
the machine principal that runs the lab cannot be told about the lab by
its own audit log. It lives beside the action names on purpose — the
writer and the reader of one stream should not learn its name from two
different files.
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

# The stream prefix both of those share. Spelled out rather than sliced
# off one of them, and pinned to both by
# `tests/unit/test_operator_audit.py::test_chaos_actions_share_the_chaos_prefix`
# — the prefix is what the read filter matches on, so it must not be able
# to drift away from the actions it is supposed to cover.
CHAOS_ACTION_PREFIX = "chaos."

# Outcome values recorded on tool invocation rows. Kept as a fixed set so
# admin filters and dashboards can rely on it.
OUTCOME_SUCCESS = "success"
OUTCOME_ERROR = "error"
OUTCOME_UNAUTHORIZED = "unauthorized"


def hidden_audit_action_prefixes(principal: Principal) -> tuple[str, ...]:
    """Audit-action prefixes `principal` must not be shown, ever.

    One entry today: the `chaos.` stream is visible only to a principal
    that holds `chaos:invoke`. The rule is keyed on the scope rather than
    on a principal name so it needs no allowlist and no configuration —
    whoever may fire the lab may read the lab, and nobody else can tell
    from a read that the lab exists.

    Why this is a read rule and not a write rule: the rows are ground
    truth for grading and operators need them (ADR 0012's invisibility is
    about the agent under test, not about the audit trail), so nothing is
    withheld from a human on the admin Audit tab — only from a machine
    principal whose whole task is to investigate a fault it must not know
    was injected. Two tokens make that separable at all: the agent's
    holds the read + action scopes, the evaluator's holds `chaos:invoke`
    (`scripts/seed_incident_commander.py` mints both).

    Callers pass the result to `AuditRepository.list_logs`'s
    `exclude_action_prefixes`, so the exclusion lands in SQL and `total`
    counts what the caller may see. Filtering in Python would leave
    `total` reporting rows that never arrive, which is a smaller version
    of the same leak: a count is a fact about the hidden rows.
    """
    if Scope.CHAOS_INVOKE.value in principal.scopes:
        return ()
    return (CHAOS_ACTION_PREFIX,)


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
) -> bool:
    """Write an `agent.tool_invoked` row for a single MCP tool call.
    Returns whether the row was staged.

    When `is_chaos=True` the action is `chaos.tool_invoked` (or
    `chaos.tool_denied` if `denied_by` is set), so chaos activity
    filters cleanly on the admin Audit tab as a separate stream from
    general agent traffic — see ADR 0008.

    Never raises — the savepoint below means a failed insert costs the
    audit row and nothing else, so the caller still holds a usable
    session and can decide what a missing row is worth. That decision is
    the caller's, not this helper's: `app.mcp.handlers` treats `False` as
    fatal to the request, because an action that took effect with no
    record of it is the failure R2-51 is about. A caller that would
    rather degrade than fail may ignore the return value — but say so at
    the call site.
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
        return False
    return True


__all__ = [
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
