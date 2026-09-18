from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from app.config import assert_chaos_gate, get_settings
from app.core import metrics
from app.core.exceptions import AppError
from app.core.logging import get_logger, request_id_var
from app.core.middleware import RequestContextMiddleware, register_route_dimension
from app.core.observability import (
    API_SERVICE_NAME,
    bootstrap_process_observability,
    instrument_app,
)
from app.core.redis import (
    close_redis_pool,
    close_sse_redis_pool,
    get_redis_client,
)
from app.workers import supervisor as worker_supervisor
from app.workers.progress_broker import reset_broker
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

_settings = get_settings()
# Shared with `app.mcp.standalone`; the MCP process ran none of it until
# WO-R2-60.
bootstrap_process_observability(
    service_name=API_SERVICE_NAME, settings=_settings
)

logger = get_logger(__name__)


def _import_eval_seeder() -> tuple[Any, Any]:
    """Resolve the eval seeder's entry points at call time — `scripts/` is an
    implicit namespace package at /app, and tests substitute the pair."""
    import sys as _sys

    if "/app" not in _sys.path:
        _sys.path.insert(0, "/app")
    from scripts.seed_eval_fixtures import (  # type: ignore[import-not-found,unused-ignore]
        seed,
        write_pins_json,
    )

    return seed, write_pins_json


async def _boot_seed_eval_fixtures() -> None:
    """SEED_EVAL_FIXTURES=true boot path. Two failure domains stay separate in
    the log: "eval fixture seed failed" (nothing landed) vs "eval fixture pins
    write failed" (fixtures landed, manifest did not). Neither blocks boot."""
    try:
        seed, write_pins_json = _import_eval_seeder()
        await seed()
    except Exception as exc:
        logger.error(
            "eval fixture seed failed",
            extra={"error_type": type(exc).__name__, "error": str(exc)[:400]},
        )
        return
    logger.info("seeded eval fixtures")

    try:
        pins_path = write_pins_json()
    except Exception as exc:
        logger.error(
            "eval fixture pins write failed — fixtures are seeded; only the "
            "pin manifest is missing",
            extra={"error_type": type(exc).__name__, "error": str(exc)[:400]},
        )
    else:
        logger.info("wrote eval fixture pins", extra={"pins_path": pins_path})


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    assert_chaos_gate(settings)
    logger.info(
        "startup",
        extra={
            "environment": settings.environment,
            "chaos_enabled": settings.chaos_enabled,
        },
    )

    # Import here to avoid circular imports at module load time
    from app.core.migration_check import assert_migrations_current
    from app.core.rls_check import assert_rls_posture
    from app.core.tenant_scope import platform_session_factory
    from app.dependencies import get_engine, get_session_factory
    from app.workers.dispatcher import worker_loop
    from app.workers.kafka_producer import start_producer, stop_producer

    # Schema comes from `alembic upgrade head` (entrypoint.sh in prod, the
    # compose `command:` in dev). A `create_all` here once left
    # alembic_version behind the tables it had built — removed.

    # Fail fast if the DB is behind the code's alembic head — the v0.4.1
    # postmortem: a missing `jobs.remediation_hint` 500'd every DLQ tool
    # for hours.
    _session_factory = get_session_factory()
    await assert_migrations_current(_session_factory)

    # Fail fast in production if this connection would bypass row-level
    # security: the runtime is the non-owner incident_app role (ADR 0015).
    # Elsewhere it only logs.
    await assert_rls_posture(_session_factory, settings)

    # Live-eval fixtures — opt-in via SEED_EVAL_FIXTURES=true. Same script as
    # `make seed-eval-fixtures`, run inline after migrations. Idempotent
    # (uuid5 ids), so re-boots are safe.
    if settings.seed_eval_fixtures:
        await _boot_seed_eval_fixtures()

    # An unreachable broker logs and does not block boot: the publish paths
    # lazily retry the start, so a boot-time outage self-heals without a
    # redeploy.
    try:
        await start_producer()
    except Exception as exc:
        logger.error("kafka producer failed to start", extra={"error": str(exc)})

    # Background CloudWatch flush; every emit_gauge/emit_count queues into
    # it. No-op outside production.
    await metrics.start_metrics_emitter()

    redis = get_redis_client()
    # Platform (cross-tenant) scope for every consumer and background loop
    # below (ADR 0026) — they are mixed-tenant by design, and
    # `tenant_isolation` refuses a statement that names no tenant. The boot
    # probes keep the strictly scoped `_session_factory` above.
    session_factory = platform_session_factory(get_engine())

    # Supervised, not fire-and-forget: this one task hosts every consumer and
    # loop, so an unwatched `create_task` means a process that serves HTTP and
    # dispatches nothing (`app/workers/supervisor.py`, ADR 0009).
    worker_supervisor.start(lambda: worker_loop(session_factory, redis))

    yield

    # `stop()` never raises: the previous `await worker_task` re-raised and
    # left the producer and both Redis pools open after a worker crash.
    await worker_supervisor.stop()

    try:
        await stop_producer()
    except Exception as exc:
        logger.error("kafka producer failed to stop", extra={"error": str(exc)})

    # Last flush before the loop closes, guarded because everything below
    # depends on getting past here.
    try:
        await metrics.stop_metrics_emitter()
    except Exception as exc:
        logger.error("metrics emitter failed to stop", extra={"error": str(exc)})

    # Both pools: the shared one every request path uses, and the dedicated
    # SSE pool the progress broker holds its one Pub/Sub connection on.
    reset_broker()
    await close_redis_pool()
    await close_sse_redis_pool()
    logger.info("shutdown")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        openapi_url=f"{settings.api_v1_prefix}/openapi.json",
        docs_url=f"{settings.api_v1_prefix}/docs",
        redoc_url=f"{settings.api_v1_prefix}/redoc",
        lifespan=lifespan,
    )

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*", "X-Request-ID", "X-Trace-ID"],
        expose_headers=["X-Request-ID", "X-Trace-ID"],
    )

    # ---------------------------------------------------------------------------
    # Exception handlers
    # ---------------------------------------------------------------------------

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error_code": exc.error_code,
                "message": exc.message,
                "details": exc.details,
                "request_id": request_id_var.get("") or None,
            },
            # None for every error that does not set them; `Retry-After` on a
            # stream-capacity refusal is the first that does.
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """Catch-all so an escaped non-AppError answers in the documented
        envelope, not Starlette's `text/plain` 500. `request_id` comes off the
        header: `RequestContextMiddleware` sets its contextvars in a child task
        this handler does not inherit.
        """
        request_id = request.headers.get("X-Request-ID") or request_id_var.get("")
        logger.exception(
            "unhandled exception",
            extra={
                "path": request.url.path,
                "method": request.method,
                "error_type": type(exc).__name__,
            },
        )
        return JSONResponse(
            status_code=500,
            content={
                "error_code": "internal_error",
                # Deliberately generic: exception text can carry connection
                # strings, row contents or internal hostnames.
                "message": "Internal server error.",
                "details": {},
                "request_id": request_id or None,
            },
        )

    # ---------------------------------------------------------------------------
    # Routers
    # ---------------------------------------------------------------------------

    from app.api import (
        admin,
        audit,
        auth,
        jobs,
        sagas,
        service_accounts,
        streaming,
    )

    prefix = settings.api_v1_prefix
    app.include_router(auth.router, prefix=prefix)
    app.include_router(jobs.router, prefix=prefix)
    app.include_router(sagas.router, prefix=prefix)
    app.include_router(admin.router, prefix=prefix)
    app.include_router(service_accounts.router, prefix=prefix)
    app.include_router(audit.router, prefix=prefix)
    app.include_router(streaming.router, prefix=prefix)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Process liveness. No I/O, no dependencies, never 503.

        What the **ALB target group** probes (`infra/alb.tf`). Pointing it at
        the deep check made one Redis outage fail every target at once, though
        every Redis path here fails open (WO-R2-65).
        """
        return {"status": "ok"}

    @app.get("/healthz/worker", include_in_schema=False)
    async def healthz_worker() -> JSONResponse:
        """Task liveness, including the in-process worker. No external I/O.

        What the **ECS container check** probes (`infra/ecs.tf`), and the only
        probe with restart authority: a worker that died is worth replacing a
        task for, a shared dependency being down is not (WO-R2-65, ADR 0009).
        """
        worker = worker_supervisor.worker_status()
        return JSONResponse(
            status_code=200 if worker.healthy else 503,
            content={
                "status": "ok" if worker.healthy else "degraded",
                "worker": "ok" if worker.healthy else "error",
                "worker_detail": worker.detail,
            },
        )

    @app.get(f"{settings.api_v1_prefix}/health", include_in_schema=False)
    async def health() -> JSONResponse:
        """Deep readiness check, for operators and dashboards.

        DB, Redis and worker liveness; 200 when all three are healthy. Nothing
        with restart or routing authority probes this (WO-R2-65) — `/healthz`
        and `/healthz/worker` ask the narrower questions they may act on.
        """
        from app.core.redis import get_redis_client
        from app.dependencies import _engine
        from sqlalchemy import text

        checks: dict[str, str] = {}

        try:
            async with _engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            checks["db"] = "ok"
        except Exception:
            checks["db"] = "error"

        try:
            redis = get_redis_client()
            await redis.ping()  # type: ignore[misc,unused-ignore]
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "error"

        worker = worker_supervisor.worker_status()
        checks["worker"] = "ok" if worker.healthy else "error"

        healthy = all(v == "ok" for v in checks.values())
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={
                "status": "ok" if healthy else "degraded",
                **checks,
                # State, restart count and last error, so an operator learns
                # whether the worker is dead, flapping or slow to heartbeat.
                "worker_detail": worker.detail,
            },
        )

    # Every route is mounted by now, so the templated route table is the
    # complete allow-list for the RequestLatency `Path` dimension.
    register_route_dimension(app)

    instrument_app(app)
    return app


app = create_app()
