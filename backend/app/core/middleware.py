"""Request middleware — the correlation ids, log context and latency metric
every request carries, and the route label the metrics are grouped by."""

import re
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from app.core import metrics
from app.core.logging import get_logger, request_id_var, trace_id_var
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = get_logger(__name__)

#: Longest caller-supplied correlation id we carry. Far below
#: `audit_logs.request_id`'s `String(255)`: an over-wide value fails the audit
#: *insert*, not the request, so on the MCP path the tool ran and left no audit
#: row (R2-51). `test_correlation_id_bound_fits_the_audit_column` pins it.
CORRELATION_ID_MAX_LENGTH = 128

#: Charset for an acceptable correlation id — UUIDs, W3C `traceparent`, X-Ray
#: ids, hex digests, base64url. An allow-list, so control characters (log
#: injection) cannot get through.
_CORRELATION_ID_RE = re.compile(rf"\A[A-Za-z0-9._:+=-]{{1,{CORRELATION_ID_MAX_LENGTH}}}\Z")


def sanitise_correlation_id(raw: str | None, *, header: str) -> str:
    """The caller-supplied `raw` if usable as a correlation id, else a fresh UUID.

    Substitution, not truncation: a truncated id correlates with nothing and lets
    many over-long headers share one `request_id`. Rejected values are never logged.
    """
    if not raw:
        return str(uuid.uuid4())
    if _CORRELATION_ID_RE.fullmatch(raw):
        return raw

    logger.warning(
        "rejected caller-supplied correlation id; generated a fresh one",
        extra={
            "header": header,
            "supplied_length": len(raw),
            "reason": (
                "too_long"
                if len(raw) > CORRELATION_ID_MAX_LENGTH
                else "illegal_characters"
            ),
        },
    )
    return str(uuid.uuid4())

#: Path dimension for a request that matched no route. Without it a scanner
#: walking random URLs would mint a distinct billable metric per URL.
UNMATCHED_ROUTE = "unmatched"


def route_label(request: Request) -> str:
    """The templated route for this request, e.g. `/jobs/{job_id}`.

    `path_format` off the matched route: bounded by the route table, unlike
    `request.url.path`. Declared path, so an included router drops its prefix.
    """
    route = request.scope.get("route")
    path_format = getattr(route, "path_format", None)
    if isinstance(path_format, str) and path_format:
        return path_format
    return UNMATCHED_ROUTE


def collect_route_labels(routes: Sequence[Any], _depth: int = 0) -> set[str]:
    """Every `path_format` reachable from a route list, recursing into routers.

    FastAPI >= 0.141 hides included routes behind
    `_IncludedRouter.original_router`; a flat scan misses the API.
    """
    labels: set[str] = set()
    if _depth > 8:  # pathological nesting; the hard cap in metrics.py backstops
        return labels

    for route in routes:
        path_format = getattr(route, "path_format", None)
        if isinstance(path_format, str) and path_format:
            labels.add(path_format)

        nested = getattr(route, "original_router", None)
        sub_routes = getattr(nested if nested is not None else route, "routes", None)
        if sub_routes:
            labels |= collect_route_labels(sub_routes, _depth + 1)

    return labels


def register_route_dimension(app: Starlette) -> None:
    """Declare the app's route table as the allow-list for the `Path` dimension.

    Call once, after every router is mounted; anything else is bucketed as
    `other`. `test_every_served_route_is_in_the_allow_list` pins the traversal.
    """
    labels = collect_route_labels(app.routes) | {UNMATCHED_ROUTE}
    metrics.register_dimension_values("Path", labels)
    logger.info("registered route dimension allow-list", extra={"routes": len(labels)})


class RequestContextMiddleware(BaseHTTPMiddleware):
    """
    Per request: validate or mint X-Request-ID / X-Trace-ID (the id reaches
    `audit_logs` — see `sanitise_correlation_id`), bind them to contextvars, log the
    access line, queue a RequestLatency metric on the templated route, and echo the
    ids back. The metric does no I/O; see `app/core/metrics.py`.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = sanitise_correlation_id(
            request.headers.get("X-Request-ID"), header="X-Request-ID"
        )
        # An absent trace header mirrors the request id; a present one is validated.
        raw_trace = request.headers.get("X-Trace-ID")
        trace_id = (
            sanitise_correlation_id(raw_trace, header="X-Trace-ID")
            if raw_trace
            else request_id
        )

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
            route = route_label(request)

            logger.info(
                "request",
                extra={
                    # Raw path in the log; only the metric dimension is templated.
                    "path": request.url.path,
                    "route": route,
                    "method": request.method,
                    "status_code": status_code,
                    "latency_ms": latency_ms,
                },
            )

            try:
                await metrics.emit_gauge(
                    "RequestLatency",
                    latency_ms,
                    unit="Milliseconds",
                    dimensions={"Path": route, "StatusCode": str(status_code)},
                )
            except Exception as exc:
                # Enqueueing is not supposed to be able to fail, but a metrics
                # bug must not become a 500 on a request that already succeeded.
                logger.warning("latency metric emit failed", extra={"error": str(exc)})

            if response is not None:
                response.headers["X-Request-ID"] = request_id
                response.headers["X-Trace-ID"] = trace_id

            request_id_var.reset(token_req)
            trace_id_var.reset(token_trace)
