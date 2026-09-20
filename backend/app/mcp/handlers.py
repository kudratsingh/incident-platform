"""
Dispatch layer — parse JSON-RPC, route to the method, enforce scope on
`tools/call`, audit every call via `record_tool_invocation`. Every failure comes
back as a JSON-RPC envelope, never a bare 500 (ADR 0010's 2026-08-30 addendum has
the transaction underneath).
"""

import time
from datetime import timedelta
from typing import Any

from app.core.exceptions import (
    AppError,
    AuthenticationError,
    AuthorizationError,
)
from app.core.logging import get_logger, request_id_var
from app.mcp import protocol as p
from app.mcp.lab_probe import LabProbeLabel, LabProbeRefused, resolve_lab_probe
from app.mcp.registry import ToolContext, get_tool, list_tools
from app.repositories.audit import AuditRepository
from app.repositories.idempotency import IdempotencyRepository
from app.services.idempotency import (
    Claim,
    IdempotencyKeyInFlightError,
    IdempotencyKeyReusedError,
    IdempotencyService,
    Replay,
)
from app.services.operator_audit import (
    OUTCOME_ERROR,
    OUTCOME_SUCCESS,
    OUTCOME_UNAUTHORIZED,
    record_tool_invocation,
)
from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)

# 24h — outlives any plausible retry window without pinning a response forever.
_IDEMPOTENCY_TTL = timedelta(hours=24)

SERVER_NAME = "incident-platform-mcp"
SERVER_VERSION = "0.1.0"
# Pinned to the version we test against; the spec is versioned per release.
SUPPORTED_PROTOCOL_VERSION = "2025-03-26"


class AuditWriteFailedError(Exception):
    """The audit row could not be written; raised so the request rolls back."""


async def _audit(audit_repo: AuditRepository, **kwargs: Any) -> None:
    """Write the tool-invocation audit row, or fail the whole request.

    `record_tool_invocation` never raises, so a dropped row used to leave a Tier-1
    action committed with nothing recording it (R2-51); raising rolls it back. Redis
    side effects cannot be unwound, but retrying under the same key is safe.
    """
    if not await record_tool_invocation(audit_repo, **kwargs):
        raise AuditWriteFailedError(str(kwargs.get("tool_name")))


def _error(
    request_id: str | int | None, code: int, message: str, data: dict[str, Any] | None = None
) -> p.JsonRpcResponse:
    return p.JsonRpcResponse(
        id=request_id,
        error=p.JsonRpcError(code=code, message=message, data=data),
    )


def _ok(request_id: str | int | None, result: dict[str, Any]) -> p.JsonRpcResponse:
    return p.JsonRpcResponse(id=request_id, result=result)


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


def handle_initialize(
    request_id: str | int | None, params: dict[str, Any]
) -> p.JsonRpcResponse:
    """Handshake. We accept any client version and echo back ours — the
    agent side decides whether to proceed based on compatibility."""
    try:
        p.InitializeParams.model_validate(params)
    except ValidationError as exc:
        return _error(
            request_id,
            p.JSONRPC_INVALID_PARAMS,
            "invalid initialize params",
            {"errors": exc.errors()},
        )

    result = p.InitializeResult(
        protocolVersion=SUPPORTED_PROTOCOL_VERSION,
        serverInfo=p.ServerInfo(name=SERVER_NAME, version=SERVER_VERSION),
    )
    return _ok(request_id, result.model_dump())


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------


def handle_tools_list(request_id: str | int | None) -> p.JsonRpcResponse:
    """Advertise every registered tool. The commander pins this, so an omission
    here is a change it cannot see."""
    tools = [
        p.ToolInfo(
            name=t.name,
            description=t.description,
            inputSchema=t.input_json_schema(),
            outputSchema=t.output_json_schema(),
            # `.value`, not the Scope member: model_dump() must emit a bare string.
            required_scope=(
                t.required_scope.value if t.required_scope is not None else None
            ),
            is_idempotent=t.is_idempotent,
        )
        for t in list_tools()
    ]
    return _ok(request_id, p.ToolsListResult(tools=tools).model_dump())


# ---------------------------------------------------------------------------
# tools/call
# ---------------------------------------------------------------------------


async def handle_tools_call(
    request_id: str | int | None,
    params: dict[str, Any],
    *,
    ctx: ToolContext,
    lab_principal_header: str | None = None,
) -> p.JsonRpcResponse:
    """Transaction envelope around a single tool call.

    Nothing escapes except `AuditWriteFailedError`, which must, so the request rolls
    back rather than committing an action nothing recorded. The work is in
    `_run_tool_call`; this wrapper covers every step of it under one handler.

    `lab_principal_header` is the raw `X-Lab-Principal` value the transport read. It is
    passed down rather than looked up here — this layer holds no request object — and it
    is consulted only when `params` carries `_lab_probe`.
    """
    try:
        return await _run_tool_call(
            request_id, params, ctx=ctx, lab_principal_header=lab_principal_header
        )
    except AuditWriteFailedError:
        # The one exception this envelope does not convert: returning a response
        # would commit a transaction whose audit row is missing (`get_db` rolls back).
        logger.exception("mcp tools/call not audited; failing the request")
        raise
    except Exception:
        # Last resort — expected failures are handled inside, with an audit row.
        logger.exception("mcp tools/call failed outside every handled path")
        return _error(
            request_id, p.JSONRPC_INTERNAL_ERROR, "internal server error"
        )


async def _run_tool_call(
    request_id: str | int | None,
    params: dict[str, Any],
    *,
    ctx: ToolContext,
    lab_principal_header: str | None = None,
) -> p.JsonRpcResponse:
    """Dispatch a tool by name. Scope is enforced here, not in the handler, and
    every branch writes an audit row."""
    audit_repo = AuditRepository(ctx.db)
    start = time.perf_counter()

    try:
        call_params = p.ToolCallParams.model_validate(params)
    except ValidationError as exc:
        return _error(
            request_id,
            p.JSONRPC_INVALID_PARAMS,
            "invalid tools/call params",
            {"errors": exc.errors()},
        )

    # `_lab_probe` is settled before the tool is even looked up, so every audit row this
    # call can write already carries the label — including the one for a tool that does
    # not exist, which is a call the lab is as capable of making as the agent is. A field
    # that cannot be honoured refuses the whole call: ignoring it would leave the row
    # labelled `agent.tool_invoked`, which is the mislabel it exists to remove
    # (WO-R3-333, ADR 0038).
    lab_label: LabProbeLabel | None = None
    if call_params.lab_probe is not None:
        try:
            lab_label = await resolve_lab_probe(
                call_params.lab_probe,
                header=lab_principal_header,
                db=ctx.db,
                caller_tenant_id=ctx.principal.tenant_id,
            )
        except LabProbeRefused as refusal:
            await _audit(
                audit_repo,
                principal=ctx.principal,
                tool_name=call_params.name,
                arguments=call_params.arguments,
                scope_used=None,
                latency_ms=(time.perf_counter() - start) * 1000,
                outcome=OUTCOME_ERROR,
                error_message=refusal.audit_message,
                request_id=request_id_var.get("") or None,
            )
            return _error(
                request_id,
                p.JSONRPC_INVALID_PARAMS,
                refusal.message,
                refusal.error_data,
            )

    # Spread into every `_audit` call below, so the label cannot be attached on one path
    # and forgotten on another.
    lab_fields: dict[str, Any] = (
        {}
        if lab_label is None
        else {
            "lab_probe_reason": lab_label.reason,
            "lab_probe_principal": lab_label.principal_name,
        }
    )

    tool_def = get_tool(call_params.name)
    if tool_def is None:
        # Audit even unknown tool attempts — useful for spotting a
        # misconfigured agent.
        await _audit(
            audit_repo,
            principal=ctx.principal,
            tool_name=call_params.name,
            arguments=call_params.arguments,
            scope_used=None,
            latency_ms=(time.perf_counter() - start) * 1000,
            outcome=OUTCOME_ERROR,
            error_message="tool not registered",
            request_id=request_id_var.get("") or None,
            **lab_fields,
        )
        return _error(
            request_id,
            p.MCP_TOOL_NOT_FOUND,
            f"unknown tool: {call_params.name}",
        )

    # Chaos tools route to the `chaos.tool_invoked` / `chaos.tool_denied`
    # audit stream (see ADR 0008), commander telemetry to `agent.run_reported`
    # (ADR 0035). Computed once so every branch below gets both.
    is_chaos = tool_def.is_chaos
    is_commander = tool_def.is_commander

    # Scope check — humans are rejected upstream by the auth dependency.
    if tool_def.required_scope is not None:
        if tool_def.required_scope.value not in ctx.principal.scopes:
            await _audit(
                audit_repo,
                principal=ctx.principal,
                tool_name=tool_def.name,
                arguments=call_params.arguments,
                scope_used=tool_def.required_scope.value,
                latency_ms=(time.perf_counter() - start) * 1000,
                outcome=OUTCOME_UNAUTHORIZED,
                error_message="missing required scope",
                request_id=request_id_var.get("") or None,
                is_chaos=is_chaos,
                is_commander=is_commander,
                denied_by="scope_check" if is_chaos else None,
                **lab_fields,
            )
            return _error(
                request_id,
                p.MCP_FORBIDDEN,
                f"missing required scope: {tool_def.required_scope.value}",
            )

    # Parse arguments against the tool's input model.
    try:
        parsed_input = tool_def.input_model.model_validate(call_params.arguments)
    except ValidationError as exc:
        await _audit(
            audit_repo,
            principal=ctx.principal,
            tool_name=tool_def.name,
            arguments=call_params.arguments,
            scope_used=tool_def.required_scope.value if tool_def.required_scope else None,
            latency_ms=(time.perf_counter() - start) * 1000,
            outcome=OUTCOME_ERROR,
            error_message="invalid arguments",
            request_id=request_id_var.get("") or None,
            is_chaos=is_chaos,
            is_commander=is_commander,
            **lab_fields,
        )
        return _error(
            request_id,
            p.JSONRPC_INVALID_PARAMS,
            "invalid tool arguments",
            {"errors": exc.errors()},
        )

    # Execute.
    scope_used = (
        tool_def.required_scope.value if tool_def.required_scope else None
    )

    # Idempotency (Tier 1 only): same key + same args replays the cached
    # response; same key + different args is a 409.
    idempotency_key: str | None = None
    idempotency_service: IdempotencyService | None = None
    claim: Claim | None = None
    if tool_def.is_idempotent:
        idempotency_service = IdempotencyService(IdempotencyRepository(ctx.db))
        idempotency_key = _extract_idempotency_key(call_params.arguments)
        if idempotency_key is None:
            await _audit(
                audit_repo,
                principal=ctx.principal,
                tool_name=tool_def.name,
                arguments=call_params.arguments,
                scope_used=scope_used,
                latency_ms=(time.perf_counter() - start) * 1000,
                outcome=OUTCOME_ERROR,
                error_message="idempotency_key required",
                request_id=request_id_var.get("") or None,
                is_chaos=is_chaos,
                is_commander=is_commander,
                **lab_fields,
            )
            return _error(
                request_id,
                p.JSONRPC_INVALID_PARAMS,
                "idempotency_key is required for this tool",
            )
        # Claim the key BEFORE executing, in one atomic INSERT ... ON CONFLICT DO
        # NOTHING. The lookup this replaces let two concurrent calls on one key both
        # run the action; winning the insert is now what authorises execution.
        try:
            acquired = await idempotency_service.acquire(
                principal=ctx.principal,
                tool_name=tool_def.name,
                idempotency_key=idempotency_key,
                arguments=call_params.arguments,
                ttl=_IDEMPOTENCY_TTL,
            )
        except (IdempotencyKeyReusedError, IdempotencyKeyInFlightError) as exc:
            await _audit(
                audit_repo,
                principal=ctx.principal,
                tool_name=tool_def.name,
                arguments=call_params.arguments,
                scope_used=scope_used,
                latency_ms=(time.perf_counter() - start) * 1000,
                outcome=OUTCOME_ERROR,
                error_message=exc.message,
                request_id=request_id_var.get("") or None,
                is_chaos=is_chaos,
                is_commander=is_commander,
                **lab_fields,
            )
            return _error(
                request_id,
                p.MCP_TOOL_ERROR,
                exc.message,
                {"error_code": exc.error_code},
            )
        if isinstance(acquired, Replay):
            hit = acquired.hit
            await _audit(
                audit_repo,
                principal=ctx.principal,
                tool_name=tool_def.name,
                arguments=call_params.arguments,
                scope_used=scope_used,
                latency_ms=(time.perf_counter() - start) * 1000,
                outcome=OUTCOME_SUCCESS,
                request_id=request_id_var.get("") or None,
                is_chaos=is_chaos,
                is_commander=is_commander,
                **lab_fields,
            )
            result = p.ToolCallResult(
                content=[
                    p.ToolCallContent(
                        text=_serialize_cached_response(hit.response)
                    )
                ]
            )
            return _ok(request_id, result.model_dump())
        claim = acquired

    # SAVEPOINT around the handler: a failing tool rolls back its own writes, so
    # the audit row below is still writable. The flush is inside it so a deferred
    # DB error (the class that sank #70) surfaces while a savepoint still exists.
    executed = False
    try:
        async with ctx.db.begin_nested():
            output = await tool_def.handler(parsed_input, ctx)
            await ctx.db.flush()
        executed = True
    except AuthenticationError as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        await _audit(
            audit_repo,
            principal=ctx.principal,
            tool_name=tool_def.name,
            arguments=call_params.arguments,
            scope_used=scope_used,
            latency_ms=latency_ms,
            outcome=OUTCOME_UNAUTHORIZED,
            error_message=exc.message,
            request_id=request_id_var.get("") or None,
            is_chaos=is_chaos,
            is_commander=is_commander,
            **lab_fields,
        )
        return _error(request_id, p.MCP_UNAUTHORIZED, exc.message)
    except AuthorizationError as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        await _audit(
            audit_repo,
            principal=ctx.principal,
            tool_name=tool_def.name,
            arguments=call_params.arguments,
            scope_used=scope_used,
            latency_ms=latency_ms,
            outcome=OUTCOME_UNAUTHORIZED,
            error_message=exc.message,
            request_id=request_id_var.get("") or None,
            is_chaos=is_chaos,
            is_commander=is_commander,
            **lab_fields,
        )
        return _error(request_id, p.MCP_FORBIDDEN, exc.message)
    except AppError as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        await _audit(
            audit_repo,
            principal=ctx.principal,
            tool_name=tool_def.name,
            arguments=call_params.arguments,
            scope_used=scope_used,
            latency_ms=latency_ms,
            outcome=OUTCOME_ERROR,
            error_message=exc.message,
            request_id=request_id_var.get("") or None,
            is_chaos=is_chaos,
            is_commander=is_commander,
            **lab_fields,
        )
        return _error(
            request_id,
            p.MCP_TOOL_ERROR,
            exc.message,
            {"error_code": exc.error_code},
        )
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        logger.exception("mcp tool crashed", extra={"tool": tool_def.name})
        # The savepoint already discarded whatever the tool staged (#5), so the
        # client's error and the database agree. Not a bare `ctx.db.rollback()` —
        # that closes `get_db`'s transaction and drops the audit write below (#6).
        await _audit(
            audit_repo,
            principal=ctx.principal,
            tool_name=tool_def.name,
            arguments=call_params.arguments,
            scope_used=scope_used,
            latency_ms=latency_ms,
            outcome=OUTCOME_ERROR,
            error_message=str(exc),
            request_id=request_id_var.get("") or None,
            is_chaos=is_chaos,
            is_commander=is_commander,
            **lab_fields,
        )
        return _error(
            request_id, p.JSONRPC_INTERNAL_ERROR, "internal tool error"
        )
    finally:
        # Release the claim on any path that will not record a response: the
        # envelope commits even when the tool failed (#154), so a claim left behind
        # wedges the key for its whole 24h TTL. `executed` marks the success path.
        if claim is not None and idempotency_service is not None and not executed:
            await _release_claim(
                ctx=ctx,
                service=idempotency_service,
                claim=claim,
                tool_name=tool_def.name,
            )

    latency_ms = (time.perf_counter() - start) * 1000
    await _audit(
        audit_repo,
        principal=ctx.principal,
        tool_name=tool_def.name,
        arguments=call_params.arguments,
        scope_used=scope_used,
        latency_ms=latency_ms,
        outcome=OUTCOME_SUCCESS,
        request_id=request_id_var.get("") or None,
        is_chaos=is_chaos,
        is_commander=is_commander,
        **lab_fields,
    )

    # Attach the response to the claim we hold so a repeat call replays it:
    # an UPDATE by id on a row this call inserted, so no race to lose.
    if idempotency_service is not None and claim is not None:
        await _complete_claim(
            ctx=ctx,
            service=idempotency_service,
            claim=claim,
            tool_name=tool_def.name,
            output=output,
        )

    # One text content block holding the JSON-serialized output model — `text`
    # is what every MCP client can parse.
    result = p.ToolCallResult(
        content=[p.ToolCallContent(text=output.model_dump_json())]
    )
    return _ok(request_id, result.model_dump())


async def _complete_claim(
    *,
    ctx: ToolContext,
    service: IdempotencyService,
    claim: Claim,
    tool_name: str,
    output: BaseModel,
) -> None:
    """Attach this call's response to the claim it already holds.

    Savepoint-wrapped for the same reason #154 wrapped the insert: the audit row for
    an action that really ran outranks the cache write, so an uncached response and
    a re-executing retry is the honest outcome.
    """
    try:
        async with ctx.db.begin_nested():
            await service.complete(
                claim,
                response=output.model_dump(mode="json"),
                ttl=_IDEMPOTENCY_TTL,
            )
    except Exception as exc:
        logger.warning(
            "tool response not cached; claim completion failed",
            extra={"tool": tool_name, "error": str(exc)},
        )


async def _release_claim(
    *,
    ctx: ToolContext,
    service: IdempotencyService,
    claim: Claim,
    tool_name: str,
) -> None:
    """Drop an unfinished claim so a retry can re-execute.

    Savepoint-wrapped and never raising: the error's own audit row still has to be
    committable. A failed release leaves the key claimed until its TTL, so log loudly.
    """
    try:
        async with ctx.db.begin_nested():
            await service.release(claim)
    except Exception as exc:
        logger.error(
            "idempotency claim not released; key stays held until its TTL",
            extra={"tool": tool_name, "error": str(exc)},
        )


def _extract_idempotency_key(arguments: dict[str, Any]) -> str | None:
    """Read `idempotency_key` out of raw arguments, before Pydantic parses them."""
    value = arguments.get("idempotency_key")
    if isinstance(value, str) and value:
        return value
    return None


def _serialize_cached_response(response: dict[str, Any]) -> str:
    """Re-serialize a cached response to the wire shape a fresh run produces."""
    import json as _json

    return _json.dumps(response, default=str)


# ---------------------------------------------------------------------------
# Top-level dispatch
# ---------------------------------------------------------------------------


async def dispatch(
    request: p.JsonRpcRequest,
    *,
    db: AsyncSession,
    redis: Redis,
    principal_or_error: Any,
    lab_principal_header: str | None = None,
) -> p.JsonRpcResponse:
    """Route a parsed JSON-RPC request to the right handler.

    `initialize` is allowed unauthenticated; every other method needs a
    `Principal` in `principal_or_error`.

    `lab_principal_header` reaches `tools/call` and nothing else: `initialize` and
    `tools/list` write no audit row, so there is nothing for a label to name.
    """

    method = request.method

    if method == "initialize":
        return handle_initialize(request.id, request.params)

    # Every other method requires auth.
    if isinstance(principal_or_error, AppError):
        return _error(
            request.id,
            p.MCP_UNAUTHORIZED,
            principal_or_error.message,
        )

    if method == "tools/list":
        return handle_tools_list(request.id)

    if method == "tools/call":
        ctx = ToolContext(db=db, redis=redis, principal=principal_or_error)
        return await handle_tools_call(
            request.id,
            request.params,
            ctx=ctx,
            lab_principal_header=lab_principal_header,
        )

    return _error(
        request.id, p.JSONRPC_METHOD_NOT_FOUND, f"unknown method: {method}"
    )


__all__ = [
    "SERVER_NAME",
    "SERVER_VERSION",
    "SUPPORTED_PROTOCOL_VERSION",
    "AuditWriteFailedError",
    "dispatch",
    "handle_initialize",
    "handle_tools_call",
    "handle_tools_list",
]
