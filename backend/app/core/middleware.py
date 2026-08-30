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

#: Path dimension for a request that matched no route. `BaseHTTPMiddleware`
#: wraps the router, so this middleware also sees 404s for arbitrary URLs —
#: a scanner walking random paths would otherwise mint a distinct billable
#: metric per URL, which puts CloudWatch cardinality under the control of
#: anyone who can reach the load balancer.
UNMATCHED_ROUTE = "unmatched"


def route_label(request: Request) -> str:
    """The templated route for this request, e.g. `/jobs/{job_id}`.

    Starlette stores the matched route object on the ASGI scope, and the scope
    dict is shared with the downstream app, so it is populated by the time
    `dispatch` regains control. `path_format` is the declared pattern with its
    converters intact — bounded by the size of the route table, unlike
    `request.url.path`, which is bounded by the number of rows in the database.

    Note the value is the route's *declared* path. FastAPI >= 0.141 nests an
    included router rather than flattening its routes into the app, so for
    anything mounted via `include_router(..., prefix="/api/v1")` the declared
    path is router-relative: `/jobs/{job_id}`, not `/api/v1/jobs/{job_id}`.
    Routes declared directly on the app (`/healthz`, `/api/v1/health`) carry
    their full path. The mix is cosmetic — every value is stable, bounded, and
    unambiguous within this app, and it is the same attribute
    `collect_route_labels` builds the allow-list from, so the two can never
    disagree about what a legitimate value looks like.
    """
    route = request.scope.get("route")
    path_format = getattr(route, "path_format", None)
    if isinstance(path_format, str) and path_format:
        return path_format
    return UNMATCHED_ROUTE


def collect_route_labels(routes: Sequence[Any], _depth: int = 0) -> set[str]:
    """Every `path_format` reachable from a route list, recursing into routers.

    FastAPI >= 0.141 represents `include_router(...)` as a single
    `_IncludedRouter` entry in `app.routes` whose real `APIRoute`s hang off
    `original_router`, so a flat scan of `app.routes` finds four docs routes and
    two health routes and misses the entire API. Mounts (`.routes`) are followed
    for the same reason.
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

    Call once, after every router is mounted. The set of templated routes is the
    exact set of `Path` values that can legitimately occur, so anything else —
    a future caller passing a raw URL, a route added at runtime and forgotten —
    is bucketed as `other` instead of billed as a new metric.

    `test_every_served_route_is_in_the_allow_list` pins this against FastAPI
    changing its route-tree shape again: if traversal ever stops finding the
    real routes, that test fails rather than production quietly reporting every
    request as `other`.
    """
    labels = collect_route_labels(app.routes) | {UNMATCHED_ROUTE}
    metrics.register_dimension_values("Path", labels)
    logger.info("registered route dimension allow-list", extra={"routes": len(labels)})


class RequestContextMiddleware(BaseHTTPMiddleware):
    """
    Runs on every request:
      1. Reads or generates X-Request-ID and X-Trace-ID headers.
      2. Binds them to contextvars so all log lines in this request carry them.
      3. Times the request and emits a structured access log.
      4. Queues a RequestLatency metric, dimensioned on the templated route.
      5. Echoes the IDs back in the response headers.

    Step 4 does no I/O. `metrics.emit_gauge` sanitises the dimensions and drops
    the datum into a bounded queue that a single background task drains on an
    interval; see `app/core/metrics.py`. This replaced a per-request
    `asyncio.create_task` around a blocking boto3 `put_metric_data`, whose
    retained task-handle set grew with in-flight emits — a slow CloudWatch was
    a memory leak on the hot path.
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
            route = route_label(request)

            logger.info(
                "request",
                extra={
                    # The raw path stays in the log, where high cardinality is
                    # free and the exact URL is what an operator needs; only the
                    # metric dimension is templated.
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
