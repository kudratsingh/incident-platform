"""The observability bootstrap every entrypoint has to run.

There are two deployables from this image — `api` (`app.main`) and `mcp`
(`app.mcp.standalone`) — and the second one never ran any of this. The
result was an agent-facing surface that emitted unstructured logs with
every INFO dropped (the root logger sat at WARNING with Python's default
formatter) and exported zero spans, while `OTLP_ENDPOINT` was configured
for it and the operator went looking for exactly that evidence when a
live run misbehaved (WO-R2-60).

So it lives here, once, and both entrypoints call it. A third entrypoint
gets it by calling one function rather than by remembering four.

**SQLAlchemy is instrumented elsewhere, on purpose.**
`app.dependencies` instruments the engine at import time, because the
instrumentation binds to the engine rather than to the process and every
entrypoint imports that module to get a session. It is listed in
`instrumented_libraries()` so a process can assert its own coverage
rather than assume it.
"""

import logging

from app.config import Settings, get_settings
from app.core.logging import setup_logging
from app.core.tracing import setup_tracing
from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor

#: Service names as they appear on spans. One per deployable, so a trace
#: spanning both processes says which one it passed through.
API_SERVICE_NAME = "incident-platform"
MCP_SERVICE_NAME = "incident-platform-mcp"

# Set once per process. Re-running `setup_tracing` is not merely wasteful:
# OTel refuses to override an installed TracerProvider and logs about it,
# and `setup_logging` replaces the root handlers, so a second pass in a
# test session would quietly reconfigure logging under whatever else is
# running. Reset it (see `tests/api/test_mcp_observability.py`) if you
# need to prove the bootstrap runs.
_bootstrapped = False


def bootstrap_process_observability(
    *, service_name: str, settings: Settings | None = None
) -> None:
    """Structured logging, tracing, and the process-wide instrumentors.

    Call at import time in the entrypoint module, before anything logs —
    a line emitted before this runs gets Python's default formatter, and
    at INFO it is not emitted at all.
    """
    global _bootstrapped
    if _bootstrapped:
        return

    resolved = settings if settings is not None else get_settings()
    setup_logging(level=resolved.log_level, log_file=resolved.log_file)
    setup_tracing(
        service_name=service_name, otlp_endpoint=resolved.otlp_endpoint
    )
    RedisInstrumentor().instrument()
    _bootstrapped = True

    logging.getLogger(__name__).info(
        "observability bootstrapped",
        extra={
            "service_name": service_name,
            "otlp_endpoint": resolved.otlp_endpoint,
            "log_level": resolved.log_level,
        },
    )


def instrument_app(app: FastAPI) -> None:
    """Server-span instrumentation for one ASGI app.

    Separate from the process bootstrap because it is per-app and has to
    run after every route is mounted — which is also why each entrypoint
    calls it at the end of its factory rather than at import.
    """
    FastAPIInstrumentor.instrument_app(app)


def instrumented_libraries(app: FastAPI | None = None) -> dict[str, bool]:
    """Which auto-instrumentations are live in this process.

    Reported rather than assumed: `test_mcp_observability.py` asserts the
    MCP process has all three, which is the check that would have caught
    WO-R2-60.

    FastAPI is reported per-app because that is how it is instrumented —
    `instrument_app` marks the app, it does not flip a process-wide flag
    the way the Redis and SQLAlchemy instrumentors do. Pass the app to
    get an answer about it.
    """
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    live = {
        "redis": RedisInstrumentor().is_instrumented_by_opentelemetry,
        "sqlalchemy": SQLAlchemyInstrumentor().is_instrumented_by_opentelemetry,
    }
    if app is not None:
        live["fastapi"] = bool(
            getattr(app, "_is_instrumented_by_opentelemetry", False)
        )
    return live


__all__ = [
    "API_SERVICE_NAME",
    "MCP_SERVICE_NAME",
    "bootstrap_process_observability",
    "instrument_app",
    "instrumented_libraries",
]
