"""
Operator audit — the immutable trail of every machine-principal action.

Every MCP tool call goes through `record_tool_invocation()`, so the row shape is uniform:
`action` is `agent.tool_invoked`, `extra_data` carries `tool_name`, `arguments`,
`scope_used`, `latency_ms`, `outcome` (Step 0 schema). `hidden_audit_action_prefixes` — the
read side of the same stream — lives here so writer and reader share one file.

One row here is not a tool call: `lab.world_reset`, written by `scripts/reset_eval_state.py`
once per reset, carrying that reset's own counters. It lives in this file for the same reason
the withholding does — the writer and the reader of a withheld stream belong together.

One row here IS a tool call, and it is in the same withheld stream: `lab.probe`, the label a
call carries when the lab made it under the agent's own token (WO-R3-333, ADR 0038). Same
writer, same shape, two extra fields in `extra_data`.
"""

import uuid
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

# A fourth stream, and the only one no tool call writes (WO-R3-327). The environment
# reset appends exactly one `lab.world_reset` row per run, whose `extra_data` is that
# reset's own summary of counters. It carries its OWN prefix rather than joining
# `chaos.` because the console reads the newest `chaos.` row as the moment the fault
# was injected: a reset filed under that prefix would be read as a fault, which is the
# opposite of what it means. `lab.` is the lab talking about the lab's own apparatus —
# withheld from the agent exactly as `chaos.` is, and for a sharper reason: the payload
# names every mechanism the reset swept.
LAB_ACTION_PREFIX = "lab."
WORLD_RESET_ACTION = "lab.world_reset"

# The second member of that stream (WO-R3-333, ADR 0038). One MCP call the lab made
# *under the agent's token* — the principal guards and the world audit do that on
# purpose, because what the agent's token can and cannot do is what they prove. The row
# is the same shape as `agent.tool_invoked` and carries two extra fields: the caller's
# own short reason and the lab credential that authorised the label. It joins `lab.`
# rather than `chaos.` for the reason the boundary row did: to the `/demo` console the
# newest `chaos.` row IS the fault, and a read is not a fault. It is withheld from the
# agent by the same prefix rule, which is the whole point — a read the agent did not
# make must not appear in the ledger the console builds from its stream.
LAB_PROBE_ACTION = "lab.probe"

# What the boundary row says it is about. There is no single resource, so
# `resource_id` stays null — the row's identity is its `created_at`.
WORLD_RESET_RESOURCE_TYPE = "world"

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

    - The `chaos.` and `lab.` streams are shown **only** to a principal holding
      `chaos:invoke`: the lab is invisible to the agent under test (ADR 0012). Two
      prefixes, one condition — whoever may fire the lab may read the lab. `chaos.`
      is the lab injecting a fault; `lab.` is the lab resetting the world it injected
      into, and its `lab.world_reset` payload names every mechanism the reset swept,
      so it leaks more than a hook name would (2026-09-20 amendment). The prefix now
      carries `lab.probe` too (WO-R3-333) and needs no second rule for it: one
      prefix, one condition, and a read the lab took under the agent's token is the
      last thing the agent should be able to read back as its own.
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
        hidden.append(LAB_ACTION_PREFIX)
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
    lab_probe_reason: str | None = None,
    lab_probe_principal: str | None = None,
) -> bool:
    """Write an `agent.tool_invoked` row for one MCP tool call; returns whether it staged.

    `is_chaos=True` uses `chaos.tool_invoked` (or `chaos.tool_denied` with `denied_by`) so
    chaos filters as its own stream (ADR 0008); `is_commander=True` uses
    `agent.run_reported` for the same reason (ADR 0035). The row shape is identical in all
    three cases, which is what lets the phase strip be rebuilt from the audit log alone:
    `extra_data.arguments` carries the run id and the state that was reported. Never
    raises — the savepoint costs only the audit row, and the caller decides:
    `app.mcp.handlers` treats `False` as fatal (R2-51).

    `lab_probe_reason` is a fourth case and the narrowest one (WO-R3-333): an honoured
    `_lab_probe` makes the row `lab.probe` **instead of `agent.tool_invoked`**, and
    instead of nothing else. A chaos call and a run report already say the lab or the
    reporter made them, and the `chaos.` stream in particular is what the console reads
    as the fault — relabelling either would move a fact rather than add one. So on those
    two the action does not move and the reason rides along in `extra_data`, which is
    where an operator looks for it anyway.
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
    # Recorded whatever the action turns out to be: the claim is a fact about the call.
    if lab_probe_reason is not None:
        extra["lab_probe_reason"] = lab_probe_reason
    if lab_probe_principal is not None:
        extra["lab_probe_principal"] = lab_probe_principal

    if is_chaos:
        action = (
            CHAOS_TOOL_DENIED_ACTION if denied_by is not None else CHAOS_TOOL_INVOKED_ACTION
        )
    elif is_commander:
        action = AGENT_RUN_REPORTED_ACTION
    elif lab_probe_reason is not None:
        action = LAB_PROBE_ACTION
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


async def record_world_reset(
    audit_repo: AuditRepository,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID | None,
    counters: dict[str, Any],
) -> None:
    """Append the one `lab.world_reset` row that closes a take (WO-R3-327).

    `counters` becomes `extra_data` verbatim — the reset's own summary, so an operator
    reading the row knows what the boundary actually did rather than only that it
    happened. The console reads this row's `created_at` as the boundary: rows and runs
    older than it belong to a previous take, which is what stops the `/demo` strip
    opening at `agent remediating` on a world that was just wiped.

    **The opposite error contract to `record_tool_invocation`, deliberately.** That one
    is savepoint-wrapped and never raises, because losing an audit row must not cost a
    tool its response. Here there is no response to protect, and a reset whose boundary
    was never written is a reset the console cannot see — it would keep reading the
    previous take's fault as current. So this raises, the caller fails loudly, and the
    operator resets again rather than recording on a page that is quietly lying.

    `principal_type` is always `service_account`: no human performs a reset. The
    evaluator's account id goes in `principal_id` when the caller can name it, and
    `None` otherwise — nullable by ADR 0007's design, so an unseeded stack still gets
    its boundary instead of an exception about attribution.
    """
    await audit_repo.log(
        WORLD_RESET_ACTION,
        tenant_id=tenant_id,
        principal_type=PRINCIPAL_TYPE_SERVICE_ACCOUNT,
        principal_id=principal_id,
        user_id=None,
        resource_type=WORLD_RESET_RESOURCE_TYPE,
        extra_data=dict(counters),
    )


__all__ = [
    "AGENT_RUN_REPORTED_ACTION",
    "CHAOS_ACTION_PREFIX",
    "CHAOS_TOOL_DENIED_ACTION",
    "CHAOS_TOOL_INVOKED_ACTION",
    "LAB_ACTION_PREFIX",
    "LAB_PROBE_ACTION",
    "OUTCOME_ERROR",
    "OUTCOME_SUCCESS",
    "OUTCOME_UNAUTHORIZED",
    "TOOL_INVOKED_ACTION",
    "WORLD_RESET_ACTION",
    "WORLD_RESET_RESOURCE_TYPE",
    "hidden_audit_action_prefixes",
    "record_tool_invocation",
    "record_world_reset",
]
