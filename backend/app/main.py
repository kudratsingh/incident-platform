import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from app.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import get_logger, request_id_var, setup_logging
from app.core.middleware import RequestContextMiddleware
from app.core.redis import close_redis_pool, get_redis_client
from app.core.tracing import setup_tracing
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor

_settings = get_settings()
setup_logging(level=_settings.log_level, log_file=_settings.log_file)
setup_tracing(service_name="incident-platform", otlp_endpoint=_settings.otlp_endpoint)
RedisInstrumentor().instrument()

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    logger.info("startup", extra={"environment": settings.environment})

    # Import here to avoid circular imports at module load time
    from app.dependencies import _engine, get_session_factory
    from app.models.base import Base
    from app.workers.dispatcher import worker_loop

    # In production, run `alembic upgrade head` before starting the app.
    # This create_all is kept only as a dev convenience for fresh environments.
    if settings.environment != "production":
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("database tables ready (dev: create_all)")

    redis = get_redis_client()
    session_factory = get_session_factory()
    worker_task = asyncio.create_task(worker_loop(session_factory, redis))

    yield

    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

    await close_redis_pool()
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
        )

    # ---------------------------------------------------------------------------
    # Routers
    # ---------------------------------------------------------------------------

    from app.api import admin, audit, auth, jobs, streaming

    prefix = settings.api_v1_prefix
    app.include_router(auth.router, prefix=prefix)
    app.include_router(jobs.router, prefix=prefix)
    app.include_router(admin.router, prefix=prefix)
    app.include_router(audit.router, prefix=prefix)
    app.include_router(streaming.router, prefix=prefix)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(f"{settings.api_v1_prefix}/health", include_in_schema=False)
    async def health() -> JSONResponse:
        """Deep health check used by ECS and load balancers.

        Verifies DB and Redis connectivity. Returns 200 if all healthy,
        503 if any dependency is down.
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
            await redis.ping()  # type: ignore[misc]
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "error"

        healthy = all(v == "ok" for v in checks.values())
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={"status": "ok" if healthy else "degraded", **checks},
        )

    FastAPIInstrumentor.instrument_app(app)
    return app


app = create_app()
