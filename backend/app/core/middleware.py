import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable

from app.core import metrics
from app.core.logging import get_logger, request_id_var, trace_id_var
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = get_logger(__name__)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """
    Runs on every request:
      1. Reads or generates X-Request-ID and X-Trace-ID headers.
      2. Binds them to contextvars so all log lines in this request carry them.
      3. Times the request and emits a structured access log.
      4. Echoes the IDs back in the response headers.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        trace_id = request.headers.get("X-Trace-ID") or request_id

        token_req = request_id_var.set(request_id)
        token_trace = trace_id_var.set(trace_id)

        start = time.perf_counter()
        response: Response | None = None
        try:
            response = await call_next(request)
            return response
        finally:
            latency_ms = round((time.perf_counter() - start) * 1000, 2)
            status_code = response.status_code if response is not None else 500

            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": status_code,
                    "latency_ms": latency_ms,
                },
            )

            # Fire-and-forget: emit latency metric without blocking the response
            asyncio.create_task(
                metrics.emit_gauge(
                    "RequestLatency",
                    latency_ms,
                    unit="Milliseconds",
                    dimensions={
                        "Path": request.url.path,
                        "StatusCode": str(status_code),
                    },
                )
            )

            if response is not None:
                response.headers["X-Request-ID"] = request_id
                response.headers["X-Trace-ID"] = trace_id

            request_id_var.reset(token_req)
            trace_id_var.reset(token_trace)
