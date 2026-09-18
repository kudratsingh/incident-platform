"""
Standalone ASGI entrypoint for the MCP process (ADR 0006).

Same image as the API, different lifecycle, one route: `POST /mcp`. This process
boots on its own, so anything the API sets up at import time must be set up here too
— `bootstrap_process_observability` is that, in one call (WO-R2-60).
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from app.config import assert_chaos_gate, get_settings
from app.core import metrics
from app.core.exceptions import AppError, AuthenticationError
from app.core.logging import get_logger
from app.core.middleware import RequestContextMiddleware, register_route_dimension
from app.core.observability import (
    MCP_SERVICE_NAME,
    bootstrap_process_observability,
    instrument_app,
)
from app.dependencies import (
    Principal,
    get_current_principal,
    get_db,
    get_redis,
)
from app.mcp import handlers, protocol
from app.mcp import tools as _tools  # noqa: F401 — side-effect: register tools
from app.utils.rate_limit import check_identity_rate_limit
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

# Before anything here logs a line or opens a span. Until WO-R2-60 this process ran
# none of it, so the agent-facing surface dropped every INFO and exported no spans.
bootstrap_process_observability(service_name=MCP_SERVICE_NAME)

logger = get_logger(__name__)

# Distinct from the API's buckets so MCP and REST allowances count separately.
MCP_RATE_BUCKET = "mcp:principal"


@asynccontextmanager
async def _mcp_lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    """Schema drift check only — no worker loop, no producer. A schema behind the
    code fails loud rather than 500ing per tool call (v0.4.1 postmortem)."""
    from app.config import get_settings
    from app.core.migration_check import assert_migrations_current
    from app.core.rls_check import assert_rls_posture
    from app.dependencies import get_session_factory

    session_factory = get_session_factory()
    await assert_migrations_current(session_factory)
    # Same RLS posture probe as the API lifespan — separate boot (ADR 0015).
    await assert_rls_posture(session_factory, get_settings())

    # It queues RequestLatency like the API does, and nothing else here drains it.
    await metrics.start_metrics_emitter()
    try:
        yield
    finally:
        await metrics.stop_metrics_emitter()


def create_mcp_app() -> FastAPI:
    """FastAPI factory for the MCP process — tests build fresh instances."""

    # Chaos triple-gate — "never in production", enforced before routes mount
    # (ADR 0008).
    assert_chaos_gate()

    # Resolved at factory time, not per request, so test overrides apply.
    settings = get_settings()

    app = FastAPI(
        title="incident-platform MCP",
        version=handlers.SERVER_VERSION,
        docs_url=None,
        redoc_url=None,
        lifespan=_mcp_lifespan,
    )
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        # AppErrors escaping dispatch still come out as JSON-RPC envelopes.
        return JSONResponse(
            status_code=exc.status_code,
            content=protocol.JsonRpcResponse(
                id=None,
                error=protocol.JsonRpcError(
                    code=_status_to_jsonrpc(exc.status_code),
                    message=exc.message,
                    data={"error_code": exc.error_code},
                ),
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """Last line of defence for the JSON-RPC contract.

        Anything escaping the dispatch layer would otherwise be Starlette's
        plain-text 500, which a client reads as a transport failure and retries —
        re-running any Tier-1 action the request already performed.
        """
        logger.exception("unhandled error on the MCP surface")
        return JSONResponse(
            status_code=500,
            # `exclude={"result"}`: JSON-RPC wants an unknown id as `"id": null`.
            content=protocol.JsonRpcResponse(
                id=None,
                error=protocol.JsonRpcError(
                    code=protocol.JSONRPC_INTERNAL_ERROR,
                    message="internal server error",
                ),
            ).model_dump(exclude={"result"}),
        )

    @app.post("/mcp", response_class=JSONResponse)
    async def mcp_endpoint(
        payload: dict[str, Any],
        request: Request,
        db: AsyncSession = Depends(get_db),
        redis: Redis = Depends(get_redis),
        principal_or_error: Principal | AppError = Depends(_principal_or_error),
    ) -> JSONResponse:
        try:
            parsed = protocol.JsonRpcRequest.model_validate(payload)
        except Exception as exc:
            resp = protocol.JsonRpcResponse(
                id=payload.get("id") if isinstance(payload, dict) else None,
                error=protocol.JsonRpcError(
                    code=protocol.JSONRPC_INVALID_REQUEST,
                    message=f"malformed JSON-RPC request: {exc}",
                ),
            )
            return JSONResponse(status_code=200, content=resp.model_dump())

        # Per-principal rate limit, between parsing and dispatch: after parsing so
        # bad framing cannot exhaust a good caller's bucket, before dispatch so the
        # refusal lands ahead of the DB pool and the tool's side effects.
        #
        # Only authenticated callers are keyed. Anonymous traffic is bounded at the
        # edge — inventing a bucket for a caller with no principal would be a
        # different control wearing this one's name.
        if isinstance(principal_or_error, Principal):
            await check_identity_rate_limit(
                redis,
                identity=principal_or_error.id,
                limit=settings.mcp_rate_limit_per_principal,
                window=settings.mcp_rate_limit_window_seconds,
                bucket=MCP_RATE_BUCKET,
            )

        response = await handlers.dispatch(
            parsed,
            db=db,
            redis=redis,
            principal_or_error=principal_or_error,
        )
        return JSONResponse(
            status_code=200,
            content=response.model_dump(exclude_none=True),
        )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # Two routes, so the `Path` allow-list here is tiny — but registering it
    # is what makes an unexpected value bucket as `other` rather than bill.
    register_route_dimension(app)

    # After routes mount, same as the API app: a server span around `POST /mcp`.
    instrument_app(app)

    return app


async def _principal_or_error(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Principal | AppError:
    """Return a failing `get_current_principal` as an `AppError` instead of raising,
    so `dispatch` can answer in-band — MCP clients expect a JSON-RPC response."""

    auth_header = request.headers.get("authorization", "")
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        # Error sentinel. `initialize` is still allowed unauthenticated.
        return AuthenticationError("missing bearer token")

    try:
        return await get_current_principal(token=token, db=db)
    except AppError as exc:
        return exc


def _status_to_jsonrpc(status: int) -> int:
    if status == 401:
        return protocol.MCP_UNAUTHORIZED
    if status == 403:
        return protocol.MCP_FORBIDDEN
    if status == 429:
        return protocol.MCP_RATE_LIMITED
    if 400 <= status < 500:
        return protocol.JSONRPC_INVALID_REQUEST
    return protocol.JSONRPC_INTERNAL_ERROR


# Module-level app for `uvicorn app.mcp.standalone:app`.
app = create_mcp_app()


__all__ = ["app", "create_mcp_app"]
