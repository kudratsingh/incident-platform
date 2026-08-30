"""
Standalone ASGI entrypoint for the MCP process.

Runs from the same image as the API — see ADR 0006. Compose brings up
a second container with `command=uvicorn app.mcp.standalone:app`; ECS
gets a sibling task definition off the same image tag. Same code, same
schemas, different lifecycle.

Public surface: exactly one route, `POST /mcp`. Everything the agent
needs is a JSON-RPC method on that endpoint.

Same code as the API means same *code*, not same behaviour by default:
this process has its own boot, so anything the API sets up at import time
has to be set up here too. `bootstrap_process_observability` below is
that, factored into one call so a third entrypoint cannot forget a piece
of it (WO-R2-60).
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

# Before anything in this process logs a line or opens a span. The MCP
# process ran none of this until WO-R2-60: the root logger sat at WARNING
# with Python's default formatter, so every INFO the agent-facing surface
# emitted was dropped and the rest was unstructured, and no TracerProvider
# was installed so it exported zero spans with `OTLP_ENDPOINT` configured
# for it. This is the surface the agent talks to — when a live run
# misbehaves, this process is where the evidence has to be.
bootstrap_process_observability(service_name=MCP_SERVICE_NAME)

logger = get_logger(__name__)

# Redis key namespace for the per-principal MCP limit. Distinct from the
# API's buckets so an agent's MCP allowance and any REST allowance it
# also holds are independent counters.
MCP_RATE_BUCKET = "mcp:principal"


@asynccontextmanager
async def _mcp_lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    """MCP-side startup — schema drift check only. The MCP process
    doesn't run the worker loop or Kafka producer; it just serves
    JSON-RPC. But it does hit the DB on every tool call, so a schema
    behind the code is fatal — see the v0.4.1 postmortem for why we
    fail loud instead of allowing 500s per call.
    """
    from app.config import get_settings
    from app.core.migration_check import assert_migrations_current
    from app.core.rls_check import assert_rls_posture
    from app.dependencies import get_session_factory

    session_factory = get_session_factory()
    await assert_migrations_current(session_factory)
    # Same RLS posture probe as the API lifespan — the MCP process shares
    # dependencies._engine's settings but boots separately, and it hits
    # the DB on every tool call (ADR 0015).
    await assert_rls_posture(session_factory, get_settings())

    # The MCP process runs the same RequestContextMiddleware as the API, so
    # it queues RequestLatency too and needs its own flush task — nothing
    # else in this process drains the queue.
    await metrics.start_metrics_emitter()
    try:
        yield
    finally:
        await metrics.stop_metrics_emitter()


def create_mcp_app() -> FastAPI:
    """FastAPI factory for the MCP process. Kept as a factory so tests
    can build fresh instances with dependency overrides without touching
    the module-level singleton."""

    # Chaos framework triple-gate — enforce the "never in production"
    # invariant before we even mount routes. See ADR 0008.
    assert_chaos_gate()

    # Resolved once, at factory time rather than per request: the
    # ceilings are deployment config, and a test that rebuilds the app
    # with overridden settings gets the overridden values.
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
        # AppErrors that escape the dispatch layer (e.g. from
        # get_current_principal itself) come out as JSON-RPC error
        # envelopes so the client sees a consistent shape.
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

        Without this, anything that escapes the dispatch layer — a
        commit that fails during dependency teardown, a bug in a
        middleware — comes back as Starlette's plain-text
        `Internal Server Error`. An MCP client can't parse that as a
        response at all, so it reads as a transport failure and gets
        retried; if the request had already run a Tier-1 action, the
        retry runs it again. Every exit from this process is an
        envelope, even the ones we didn't see coming.
        """
        logger.exception("unhandled error on the MCP surface")
        return JSONResponse(
            status_code=500,
            # `exclude={"result"}` rather than `exclude_none=True`: the
            # request id is unknowable this far out, and JSON-RPC wants
            # that said as an explicit `"id": null`, not by omitting the
            # member.
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

        # Per-principal rate limit, between parsing and dispatch.
        #
        # After parsing so a malformed body still gets its JSON-RPC
        # parse error rather than a 429 (the request never reached a
        # tool, and charging it to the principal's bucket would let bad
        # framing exhaust a good caller's allowance). Before dispatch
        # because dispatch is where the DB pool and the tool side
        # effects are — the whole point is to refuse before the work.
        #
        # Only authenticated callers are keyed: `principal_or_error` is
        # an `AppError` for anonymous requests, and `initialize` is
        # deliberately allowed unauthenticated (handled inside
        # dispatch). Anonymous traffic is bounded at the edge, not here;
        # this limiter's contract is per-principal, and inventing a
        # bucket for callers who have no principal would be a different
        # control wearing this one's name.
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

    # After every route is mounted, same as the API app: this is what puts
    # a server span around each `POST /mcp` and makes the SQLAlchemy and
    # Redis spans underneath it hang off something.
    instrument_app(app)

    return app


async def _principal_or_error(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Principal | AppError:
    """Auth wrapper — normalize a failing `get_current_principal` into a
    returned `AppError` rather than a raised one, so `dispatch` can
    convert it to a JSON-RPC error envelope in-band. The standalone
    entrypoint prefers an in-band error over an HTTP 401 because MCP
    clients expect a valid JSON-RPC response for every request."""

    auth_header = request.headers.get("authorization", "")
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        # No token — return an error sentinel. `initialize` is still
        # allowed unauthenticated (handled inside dispatch); other
        # methods will get a JSON-RPC unauthorized error.
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
