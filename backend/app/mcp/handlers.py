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

Every one of those is a JSON-RPC envelope: `tools/call` is wrapped end to
end, so no exception — including one raised after the tool has already
run — can reach the transport as a bare 500. See ADR 0010's 2026-08-30
addendum for what the transaction looks like underneath.
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
            outputSchema=t.output_json_schema(),
            # `.value` rather than the Scope member: model_dump() has to
            # produce a bare JSON string, not an enum repr, or the
            # commander's snapshot diffs on the serialization instead of
            # on the scope.
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
) -> p.JsonRpcResponse:
    """Transaction envelope around a single tool call.

    Nothing gets out of here except a `JsonRpcResponse`. The work is in
    `_run_tool_call`; this wrapper exists so that *every* step of it —
    argument validation, execution, the audit write, the idempotency
    store — is covered by one handler. It used to be that the
    post-execution block sat past the end of the last `except`, so an
    exception there (an idempotency-key collision, most plausibly)
    unwound straight out of `dispatch` into Starlette. `get_db` saw the
    exception on the way through and rolled the request transaction
    back, taking the success audit row for an action that had already
    run with it, and the client got a plain-text 500 it could only read
    as a transport failure — so it retried, and the Tier-1 side effect
    happened a second time with no audit row for either attempt.
    """
    try:
        return await _run_tool_call(request_id, params, ctx=ctx)
    except Exception:
        # Deliberately last-resort. Every *expected* failure is handled
        # inside with an audit row attached; reaching here means an
        # unhandled one, and the response still has to be an envelope.
        logger.exception("mcp tools/call failed outside every handled path")
        return _error(
            request_id, p.JSONRPC_INTERNAL_ERROR, "internal server error"
        )


async def _run_tool_call(
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
    claim: Claim | None = None
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
        # Claim the key BEFORE executing, in one atomic INSERT ... ON
        # CONFLICT DO NOTHING. The lookup this replaces sat in the same
        # READ COMMITTED transaction as the insert that claimed the key,
        # with the whole action in between: two concurrent calls on one
        # key both missed the cache, both ran the action, and the loser
        # then died on the unique constraint with its side effect already
        # landed. Winning the insert is now what authorises execution, so
        # the second caller never gets that far.
        try:
            acquired = await idempotency_service.acquire(
                principal=ctx.principal,
                tool_name=tool_def.name,
                idempotency_key=idempotency_key,
                arguments=call_params.arguments,
                ttl=_IDEMPOTENCY_TTL,
            )
        except (IdempotencyKeyReusedError, IdempotencyKeyInFlightError) as exc:
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
        if isinstance(acquired, Replay):
            hit = acquired.hit
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
        claim = acquired

    # SAVEPOINT around the handler. A tool that fails for any reason
    # rolls back its own partial writes and nothing else: the request
    # transaction stays open and usable, which is what lets the audit
    # row below actually be written. The flush is inside the savepoint
    # on purpose — a deferred DB error (constraint violation, FK drift —
    # the class that sank #70) has to surface while there is still a
    # savepoint to roll back to, not at the outer commit where the
    # response has already been decided.
    executed = False
    try:
        async with ctx.db.begin_nested():
            output = await tool_def.handler(parsed_input, ctx)
            await ctx.db.flush()
        executed = True
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
        # The savepoint above already discarded whatever the tool staged
        # before it died (#5), so the client's "internal tool error" and
        # the database now agree. This used to be `await ctx.db.rollback()`,
        # which closed the transaction `get_db` opened as a context
        # manager: SQLAlchemy then refused every later statement with
        # "Can't operate on closed transaction inside context manager",
        # so the audit write below — savepoint-wrapped and silent on
        # failure (#6) — was dropped to the log on every crashed call.
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
    finally:
        # Release the claim on every path that will not go on to record a
        # response. The envelope deliberately commits the request
        # transaction even when the tool failed, so that the
        # `outcome=error` audit row survives (#154) — which means a claim
        # left behind commits with it and wedges the key for its whole
        # 24h TTL, turning one failed call into a permanently unusable
        # key. A retry has to be able to re-execute.
        #
        # `executed` is set at the end of the try body, so the success
        # path reaches `complete` below instead of releasing here.
        if claim is not None and idempotency_service is not None and not executed:
            await _release_claim(
                ctx=ctx,
                service=idempotency_service,
                claim=claim,
                tool_name=tool_def.name,
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

    # Attach the response to the claim we already hold, so a repeat call
    # with the same key returns this result verbatim. An UPDATE by id on
    # a row this call inserted, so unlike the insert-after-execution it
    # replaces, it cannot lose a race for the key — there is no race left
    # to lose.
    if idempotency_service is not None and claim is not None:
        await _complete_claim(
            ctx=ctx,
            service=idempotency_service,
            claim=claim,
            tool_name=tool_def.name,
            output=output,
        )

    # Emit the tool result as a single text content block whose body is
    # the JSON-serialized output model. Structured content is the norm
    # for our tools; the MCP `text` content type is a lowest-common
    # denominator that every client can parse.
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

    An UPDATE by primary key on a row this call inserted, so the
    duplicate-key error that used to land here — a concurrent caller, or
    an expired-but-unreaped record still occupying the unique index —
    cannot happen: both are settled before the action runs now.

    Still savepoint-wrapped, for the same reason #154 wrapped the insert.
    The transaction at this point already holds the audit row for a
    Tier-1 action that really did execute; that row is the agent's safety
    grade (`evals/guards.py` reads `agent.tool_invoked`), so it outranks
    the cache write. If the update somehow fails, Postgres would poison
    the transaction and take the audit row down with it unless there is a
    savepoint to roll back to. The response goes uncached and a retry
    re-executes — the honest outcome, and strictly better than losing the
    evidence that the first attempt ran.
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

    Savepoint-wrapped and never raising: this runs in a `finally` on
    paths that are already returning an error response, and the audit row
    for that error still has to be committable afterwards. A release that
    cannot be written leaves the key claimed until its TTL — logged
    loudly, because that is the one state where a retry gets
    `idempotency_key_in_flight` for a call nobody is running.
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
