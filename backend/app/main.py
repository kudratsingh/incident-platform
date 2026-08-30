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
# Shared with `app.mcp.standalone` — see `app/core/observability.py`. The
# MCP process ran none of this until WO-R2-60, which is why it is one
# function now rather than four lines an entrypoint can half-copy.
bootstrap_process_observability(
    service_name=API_SERVICE_NAME, settings=_settings
)

logger = get_logger(__name__)


def _import_eval_seeder() -> tuple[Any, Any]:
    """Resolve the eval seeder's entry points at call time.

    `scripts/` is bind-mounted (dev) or baked (image) at /app and picked
    up as an implicit namespace package (no __init__.py). Split out of
    the boot path so tests can substitute the (seed, write_pins_json)
    pair without faking a package tree."""
    import sys as _sys

    if "/app" not in _sys.path:
        _sys.path.insert(0, "/app")
    from scripts.seed_eval_fixtures import (  # type: ignore[import-not-found,unused-ignore]
        seed,
        write_pins_json,
    )

    return seed, write_pins_json


async def _boot_seed_eval_fixtures() -> None:
    """SEED_EVAL_FIXTURES=true boot path, with its two failure domains
    kept unconflatable in the log:

      * "eval fixture seed failed"       — the fixtures did NOT land.
      * "eval fixture pins write failed" — the fixtures DID land; only
        the pin manifest is missing.

    They used to share one try/except, so the EACCES from the pins write
    (whose old default lived under the root-owned /app) was reported as
    a seed failure while 5 alerts, 9 jobs and 6 deploy markers sat
    committed in the database — anyone reading the log concluded the
    fixtures were absent when they were present. Neither failure blocks
    boot: worst case the tools serve an unseeded (or unpinned) world,
    which every tool already handles defensively."""
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
    from app.dependencies import get_session_factory
    from app.workers.dispatcher import worker_loop
    from app.workers.kafka_producer import start_producer, stop_producer

    # Schema is materialised by `alembic upgrade head`, run from
    # scripts/entrypoint.sh in prod and from the docker-compose
    # `command:` in dev. A previous version of this lifespan also called
    # `Base.metadata.create_all` here as a "dev convenience," which
    # created a Frankenstein state on first boot: create_all built every
    # current table but the alembic_version row still pointed at the
    # initial revision, so the next alembic run would find tables already
    # existing and refuse to advance. Removed.

    # Fail fast if the DB is behind the code's alembic head. The v0.4.1
    # postmortem: `docker compose restart` didn't rerun the migrate
    # one-shot, so `jobs.remediation_hint` was missing and every DLQ
    # tool 500'd for hours before an operator spotted it in the logs.
    # A loud boot failure is strictly better than the silent run.
    _session_factory = get_session_factory()
    await assert_migrations_current(_session_factory)

    # Fail fast (in production) if this connection would silently bypass
    # row-level security — superuser, or table owner without FORCE. The
    # runtime is supposed to be the non-owner incident_app role (ADR
    # 0015); outside production the probe only logs, so local superuser
    # compose stacks keep booting.
    await assert_rls_posture(_session_factory, settings)

    # Live-eval fixtures — opt-in via SEED_EVAL_FIXTURES=true. Runs the
    # same script the operator would invoke via `make seed-eval-fixtures`,
    # inline in the lifespan so the agent's `docker compose up` produces
    # a stack with realistic data without a separate step. Idempotent —
    # every ID is `uuid5`-derived, so re-boots are safe. Runs after
    # migrations (which the compose command executes first) so the
    # deploy_markers / alerts / etc. tables exist.
    if settings.seed_eval_fixtures:
        await _boot_seed_eval_fixtures()

    # Kafka producer — if the broker is unreachable we log and continue so the
    # API still boots. The producer stays unset, and the publish paths lazily
    # retry the start (throttled to one attempt per 5s), so the outbox relay's
    # next tick after the broker returns restarts it: a boot-time broker outage
    # self-heals without a redeploy.
    try:
        await start_producer()
    except Exception as exc:
        logger.error("kafka producer failed to start", extra={"error": str(exc)})

    # Background CloudWatch flush task. Every emit_gauge/emit_count call in
    # this process (request middleware, worker metrics loop) queues into it
    # rather than making its own PutMetricData call. No-op outside production.
    await metrics.start_metrics_emitter()

    redis = get_redis_client()
    session_factory = _session_factory

    # Supervised, not fire-and-forget. This one task hosts every consumer and
    # every background loop — there is no separate worker deployable yet — so
    # an unwatched `create_task` here means a process that answers HTTP while
    # dispatching nothing, with `/api/v1/health` still green. The supervisor
    # restarts it with capped backoff and publishes the liveness the deep
    # health check below reads (`app/workers/supervisor.py`, ADR 0009).
    worker_supervisor.start(lambda: worker_loop(session_factory, redis))

    yield

    # `stop()` cancels the worker, waits for its in-flight drain, and never
    # raises. It has to never raise: the previous `await worker_task` re-raised
    # whatever the worker had stored, which aborted the lifespan right here and
    # left the Kafka producer and both Redis pools open on every shutdown that
    # followed a worker crash.
    await worker_supervisor.stop()

    try:
        await stop_producer()
    except Exception as exc:
        logger.error("kafka producer failed to stop", extra={"error": str(exc)})

    # Last flush before the loop closes, so the final window is not discarded.
    # Guarded for the same reason as the worker await: everything below it —
    # the broker reset and both pool closes — depends on getting past here.
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
        """Catch-all so an escaped non-AppError still answers in the documented
        envelope (`error_code` / `message` / `details` / `request_id`) instead of
        Starlette's bare `text/plain` "Internal Server Error".

        The error shape a client is most likely to meet during an incident was
        the one shape it could not parse, and the response carried no
        correlation ID — so the 500 a user reported could not be tied back to a
        log line. Starlette re-raises after this handler runs, so uvicorn still
        logs the traceback and the error still reaches OTel; only the bytes on
        the wire change.

        `request_id_var` is read off the header rather than the contextvar:
        `RequestContextMiddleware` is a `BaseHTTPMiddleware`, so its contextvar
        assignments happen in a child task that this handler does not inherit.
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
        return {"status": "ok"}

    @app.get(f"{settings.api_v1_prefix}/health", include_in_schema=False)
    async def health() -> JSONResponse:
        """Deep health check used by ECS and load balancers.

        Verifies DB connectivity, Redis connectivity **and worker liveness**.
        Returns 200 if all three are healthy, 503 otherwise.

        The worker check changes what a green answer here means. It used to
        mean "this process can reach its dependencies"; it now means "…and it
        is processing jobs". That distinction was the whole failure: the
        worker task runs inside this process (there is no separate worker
        deployable), so a worker that died at boot left every probe green
        while the backlog built with nothing draining it — and `ConsumerLag`,
        the metric both backlog alarms read, is emitted by a loop inside the
        dead worker and is deliberately not emitted when lag is unknown. A
        dead worker makes it go *absent*, and both alarms read missing data as
        `notBreaching`.

        Both probes that consume this endpoint act on it: the ECS container
        check (`infra/ecs.tf`) and the ALB target group (`infra/alb.tf`), each
        30s apart with a 3-failure threshold. A worker that recovers does so
        well inside that window (the restart ladder caps at 30s); one that
        cannot gets the task recycled, which is the correct outcome.

        The worker probe is in-process and I/O-free, so it adds nothing to the
        endpoint's latency.
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
                # State, restart count and the last error, so the operator who
                # curls this during an incident learns whether the worker is
                # dead, flapping or merely slow to heartbeat — without it,
                # `"worker": "error"` sends them to the logs for the next step.
                "worker_detail": worker.detail,
            },
        )

    # Every route is mounted by now, so the templated route table is the
    # complete allow-list for the RequestLatency `Path` dimension.
    register_route_dimension(app)

    instrument_app(app)
    return app


app = create_app()
