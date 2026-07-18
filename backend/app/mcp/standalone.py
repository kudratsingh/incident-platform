"""
Standalone ASGI entrypoint for the MCP process.

Runs from the same image as the API — see ADR 0006. Compose brings up
a second container with `command=uvicorn app.mcp.standalone:app`; ECS
gets a sibling task definition off the same image tag. Same code, same
schemas, different lifecycle.

Public surface: exactly one route, `POST /mcp`. Everything the agent
needs is a JSON-RPC method on that endpoint.
"""

from typing import Any

from app.core.exceptions import AppError, AuthenticationError
from app.core.logging import get_logger
from app.core.middleware import RequestContextMiddleware
from app.dependencies import (
    Principal,
    get_current_principal,
    get_db,
    get_redis,
)
from app.mcp import handlers, protocol
from app.mcp import tools as _tools  # noqa: F401 — side-effect: register tools
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)


def create_mcp_app() -> FastAPI:
    """FastAPI factory for the MCP process. Kept as a factory so tests
    can build fresh instances with dependency overrides without touching
    the module-level singleton."""

    app = FastAPI(
        title="incident-platform MCP",
        version=handlers.SERVER_VERSION,
        docs_url=None,
        redoc_url=None,
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
    if 400 <= status < 500:
        return protocol.JSONRPC_INVALID_REQUEST
    return protocol.JSONRPC_INTERNAL_ERROR


# Module-level app for `uvicorn app.mcp.standalone:app`.
app = create_mcp_app()


__all__ = ["app", "create_mcp_app"]
