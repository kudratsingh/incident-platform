"""
Dispatch layer — parse JSON-RPC, route to method, enforce scope on
tools/call, wrap every call in `record_tool_invocation` for the audit
trail. This is the layer that turns a bare-metal MCP request into a
scope-checked, audited service-layer call.

Error mapping to JSON-RPC codes:
  - `ValidationError` (Pydantic)        → JSONRPC_INVALID_PARAMS
  - `AuthenticationError` (AppError)    → MCP_UNAUTHORIZED
  - `AuthorizationError` (AppError)     → MCP_FORBIDDEN
  - unknown tool                        → MCP_TOOL_NOT_FOUND
  - other `AppError`                    → MCP_TOOL_ERROR
  - anything else                       → JSONRPC_INTERNAL_ERROR
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
from app.mcp.registry import ToolContext, get_tool, list_tools
from app.repositories.audit import AuditRepository
from app.repositories.idempotency import IdempotencyRepository
from app.services.idempotency import (
    IdempotencyKeyReusedError,
    IdempotencyService,
)
from app.services.operator_audit import (
    OUTCOME_ERROR,
    OUTCOME_SUCCESS,
    OUTCOME_UNAUTHORIZED,
    record_tool_invocation,
)
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)

# Cached Tier-1 responses outlive any plausible retry window at 24h but
# stop pinning the platform to a response forever — a leftover record with
# no TTL is why repeat operator restores replayed stale results.
_IDEMPOTENCY_TTL = timedelta(hours=24)

SERVER_NAME = "incident-platform-mcp"
SERVER_VERSION = "0.1.0"
# The MCP spec is still versioned per calendar release. We echo the client's
# requested version if we support it; otherwise pin to the version we test
# against.
SUPPORTED_PROTOCOL_VERSION = "2025-03-26"


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
    tools = [
        p.ToolInfo(
            name=t.name,
            description=t.description,
            inputSchema=t.input_json_schema(),
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
) -> p.JsonRpcResponse:
    """Dispatch a tool by name. This is where scope enforcement lives —
    the tool handler itself never sees the check. Every branch writes an
    audit row via `record_tool_invocation` so operators can filter by
    outcome on the Audit tab."""
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

    tool_def = get_tool(call_params.name)
    if tool_def is None:
        # Audit even unknown tool attempts — useful for spotting a
        # misconfigured agent.
        await record_tool_invocation(
            audit_repo,
            principal=ctx.principal,
            tool_name=call_params.name,
            arguments=call_params.arguments,
            scope_used=None,
            latency_ms=(time.perf_counter() - start) * 1000,
            outcome=OUTCOME_ERROR,
            error_message="tool not registered",
            request_id=request_id_var.get("") or None,
        )
        return _error(
            request_id,
            p.MCP_TOOL_NOT_FOUND,
            f"unknown tool: {call_params.name}",
        )

    # Chaos tools route to the `chaos.tool_invoked` / `chaos.tool_denied`
    # audit stream (see ADR 0008). Compute once so every branch gets it.
    is_chaos = tool_def.is_chaos

    # Scope check. Machine-only surface — humans presenting a JWT are
    # rejected upstream by the auth dependency, but a machine principal
    # missing the specific scope hits this branch.
    if tool_def.required_scope is not None:
        if tool_def.required_scope.value not in ctx.principal.scopes:
            await record_tool_invocation(
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
                denied_by="scope_check" if is_chaos else None,
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
        await record_tool_invocation(
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

    # Idempotency check (Tier 1 actions only). A repeat call with the
    # same (tenant, principal, key) + matching arguments returns the
    # cached response without invoking the handler. Same key +
    # different args refuses with IdempotencyKeyReusedError (409).
    idempotency_key: str | None = None
    idempotency_service: IdempotencyService | None = None
    if tool_def.is_idempotent:
        idempotency_service = IdempotencyService(IdempotencyRepository(ctx.db))
        idempotency_key = _extract_idempotency_key(call_params.arguments)
        if idempotency_key is None:
            await record_tool_invocation(
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
            )
            return _error(
                request_id,
                p.JSONRPC_INVALID_PARAMS,
                "idempotency_key is required for this tool",
            )
        try:
            hit = await idempotency_service.lookup(
                principal=ctx.principal,
                tool_name=tool_def.name,
                idempotency_key=idempotency_key,
                arguments=call_params.arguments,
            )
        except IdempotencyKeyReusedError as exc:
            await record_tool_invocation(
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
            )
            return _error(
                request_id,
                p.MCP_TOOL_ERROR,
                exc.message,
                {"error_code": exc.error_code},
            )
        if hit is not None:
            await record_tool_invocation(
                audit_repo,
                principal=ctx.principal,
                tool_name=tool_def.name,
                arguments=call_params.arguments,
                scope_used=scope_used,
                latency_ms=(time.perf_counter() - start) * 1000,
                outcome=OUTCOME_SUCCESS,
                request_id=request_id_var.get("") or None,
                is_chaos=is_chaos,
            )
            result = p.ToolCallResult(
                content=[
                    p.ToolCallContent(
                        text=_serialize_cached_response(hit.response)
                    )
                ]
            )
            return _ok(request_id, result.model_dump())

    try:
        output = await tool_def.handler(parsed_input, ctx)
    except AuthenticationError as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        await record_tool_invocation(
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
        )
        return _error(request_id, p.MCP_UNAUTHORIZED, exc.message)
    except AuthorizationError as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        await record_tool_invocation(
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
        )
        return _error(request_id, p.MCP_FORBIDDEN, exc.message)
    except AppError as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        await record_tool_invocation(
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
        # Discard any partial writes the tool staged before it crashed
        # (#5). Without this rollback, the outer get_db() cleanup would
        # commit those writes behind our "internal tool error" response
        # — the client sees failure while the DB kept the half-executed
        # effect. The audit below is savepoint-wrapped (#6); if it fails
        # against the rolled-back session it drops silently to the log.
        try:
            await ctx.db.rollback()
        except Exception:
            logger.exception("mcp handler rollback after tool crash failed")
        await record_tool_invocation(
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
        )
        return _error(
            request_id, p.JSONRPC_INTERNAL_ERROR, "internal tool error"
        )

    latency_ms = (time.perf_counter() - start) * 1000
    await record_tool_invocation(
        audit_repo,
        principal=ctx.principal,
        tool_name=tool_def.name,
        arguments=call_params.arguments,
        scope_used=scope_used,
        latency_ms=latency_ms,
        outcome=OUTCOME_SUCCESS,
        request_id=request_id_var.get("") or None,
        is_chaos=is_chaos,
    )

    # Persist the response under the idempotency key so a repeat call
    # with the same key returns the same result verbatim.
    if idempotency_service is not None and idempotency_key is not None:
        await idempotency_service.store(
            principal=ctx.principal,
            tool_name=tool_def.name,
            idempotency_key=idempotency_key,
            arguments=call_params.arguments,
            response=output.model_dump(mode="json"),
            ttl=_IDEMPOTENCY_TTL,
        )

    # Force the session to send pending SQL so any deferred DB error
    # (constraint violation, FK drift — the class that sank #70) surfaces
    # here as an exception rather than silently at the outer commit. That
    # way a success response only goes out if the writes actually
    # reached the DB successfully; a failure at flush lands in the
    # `except Exception` handler above and the whole tx is rolled back.
    # See ADR 0010's commit-before-response section.
    await ctx.db.flush()

    # Emit the tool result as a single text content block whose body is
    # the JSON-serialized output model. Structured content is the norm
    # for our tools; the MCP `text` content type is a lowest-common
    # denominator that every client can parse.
    result = p.ToolCallResult(
        content=[p.ToolCallContent(text=output.model_dump_json())]
    )
    return _ok(request_id, result.model_dump())


def _extract_idempotency_key(arguments: dict[str, Any]) -> str | None:
    """Idempotent tools declare `idempotency_key: str` in their input
    model. We read the value out of raw arguments (pre-Pydantic parse)
    so the check can run before/after model validation without
    ambiguity."""
    value = arguments.get("idempotency_key")
    if isinstance(value, str) and value:
        return value
    return None


def _serialize_cached_response(response: dict[str, Any]) -> str:
    """Re-serialize a cached response to the same wire shape a fresh
    execution would produce. `default=str` mirrors what
    `model_dump_json()` does for datetimes."""
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
) -> p.JsonRpcResponse:
    """Route a parsed JSON-RPC request to the right handler.

    `principal_or_error` is either a `Principal` (auth succeeded) or an
    `AppError` (auth failed). We only fail the request out for auth-
    requiring methods; `initialize` is allowed unauthenticated so the
    agent can complete the handshake before minting its scoped token
    if it ever wants to."""

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
        return await handle_tools_call(request.id, request.params, ctx=ctx)

    return _error(
        request.id, p.JSONRPC_METHOD_NOT_FOUND, f"unknown method: {method}"
    )


__all__ = [
    "SERVER_NAME",
    "SERVER_VERSION",
    "SUPPORTED_PROTOCOL_VERSION",
    "dispatch",
    "handle_initialize",
    "handle_tools_call",
    "handle_tools_list",
]
