"""The observability bootstrap every entrypoint has to run.

Both deployables (`app.main`, `app.mcp.standalone`) call it; the MCP process ran
none of it and emitted unstructured logs with every INFO dropped and zero spans
while `OTLP_ENDPOINT` was set (WO-R2-60). **SQLAlchemy is instrumented elsewhere**,
in `app.dependencies`, because it binds to the engine rather than the process; it
is listed in `instrumented_libraries()` so a process can assert its own coverage.
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

# Set once per process: OTel refuses to replace an installed TracerProvider,
# and `setup_logging` swaps the root handlers (reset it in
# `tests/api/test_mcp_observability.py`).
_bootstrapped = False


def bootstrap_process_observability(
    *, service_name: str, settings: Settings | None = None
) -> None:
    """Structured logging, tracing, and the process-wide instrumentors.

    Call at import time, before anything logs.
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

    Per-app, so call it after every route is mounted.
    """
    FastAPIInstrumentor.instrument_app(app)


def instrumented_libraries(app: FastAPI | None = None) -> dict[str, bool]:
    """Which auto-instrumentations are live in this process.

    Reported rather than assumed: `test_mcp_observability.py` asserts the MCP
    process has all three (WO-R2-60). FastAPI is per-app — pass the app.
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
