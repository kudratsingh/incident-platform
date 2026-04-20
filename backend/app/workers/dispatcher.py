"""
Worker dispatcher — the heart of Phase 2.

Responsibilities:
  1. Poll the Redis queue for pending jobs (every POLL_INTERVAL seconds).
  2. Promote delayed-retry jobs whose backoff has elapsed.
  3. Dispatch each job to the correct processor based on job type.
  4. Handle retries with exponential backoff.
  5. Move exhausted jobs to dead_letter status.
  6. Publish progress events throughout.

Concurrency model selection (this is the core design decision):
  ┌──────────────────┬──────────────┬───────────────────────────────────────┐
  │ Job type         │ Model        │ Why                                   │
  ├──────────────────┼──────────────┼───────────────────────────────────────┤
  │ bulk_api_sync    │ asyncio      │ Many concurrent I/O calls, no GIL     │
  │ csv_upload       │ threading    │ Blocking file I/O / non-async SDK     │
  │ doc_analysis     │ multiprocess │ CPU-bound, GIL must be escaped        │
  │ report_gen       │ multiprocess │ CPU-bound, GIL must be escaped        │
  └──────────────────┴──────────────┴───────────────────────────────────────┘
"""

import asyncio
import uuid
from typing import Any

from app.config import get_settings
from app.core import metrics
from app.core.logging import get_logger, job_id_var, trace_id_var
from app.core.tracing import extract_context, get_tracer
from app.models.enums import JobStatus, JobType
from app.repositories.audit import AuditRepository
from app.repositories.job import JobRepository
from app.workers import async_tasks, cpu_processors, progress, queue, thread_adapters
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = get_logger(__name__)
tracer = get_tracer(__name__)

POLL_INTERVAL = 0.5  # seconds between queue checks
MAX_CONCURRENT_JOBS = 10  # cap on simultaneously running jobs

# Strategy map: job type → processor coroutine
_PROCESSORS = {
    JobType.BULK_API_SYNC: async_tasks.process_bulk_api_sync,
    JobType.CSV_UPLOAD: thread_adapters.process_csv_upload,
    JobType.DOC_ANALYSIS: cpu_processors.process_doc_analysis,
    JobType.REPORT_GEN: cpu_processors.process_report_gen,
}


async def _run_job(
    job_id_str: str,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Any,
) -> None:
    """Fetch, execute, and finalize a single job. Handles retry / dead-letter."""
    settings = get_settings()
    job_id = uuid.UUID(job_id_str)
    token = job_id_var.set(job_id_str)

    # ------------------------------------------------------------------ #
    # 1. Load job and mark RUNNING                                         #
    # ------------------------------------------------------------------ #
    async with session_factory() as session:
        async with session.begin():
            repo = JobRepository(session)
            job = await repo.get_by_id(job_id)
            if not job:
                logger.warning("job not found, skipping", extra={"job_id": job_id_str})
                job_id_var.reset(token)
                return
            if job.status not in (JobStatus.PENDING,):
                # Could have been replayed or cancelled between pop and now
                logger.info(
                    "job no longer pending, skipping",
                    extra={"job_id": job_id_str, "status": job.status},
                )
                job_id_var.reset(token)
                return

            trace_id_var.set(job.trace_id or job_id_str)
            payload = dict(job.payload or {})
            job_type = job.type
            retry_count = job.retry_count
            max_retries = job.max_retries

            await repo.update_status(job_id, JobStatus.RUNNING)

    await progress.publish(redis, job_id_str, "running", 0, "Job started")
    logger.info("job started", extra={"type": job_type, "retry_count": retry_count})

    # Restore the OTel trace context that was injected at job creation time so
    # this span becomes a child of the original HTTP request span.
    otel_carrier: dict[str, str] = payload.pop("__traceparent", {})
    parent_ctx = extract_context(otel_carrier) if otel_carrier else None

    # ------------------------------------------------------------------ #
    # 2. Execute processor                                                  #
    # ------------------------------------------------------------------ #
    processor = _PROCESSORS.get(JobType(job_type))
    if processor is None:
        async with session_factory() as session:
            async with session.begin():
                await JobRepository(session).update_status(
                    job_id, JobStatus.FAILED,
                    extra={"error_message": f"No processor for type: {job_type}"},
                )
        await progress.publish(redis, job_id_str, "failed", 0, f"Unknown job type: {job_type}")
        job_id_var.reset(token)
        return

    async def _publish(pct: int, message: str) -> None:
        await progress.publish(redis, job_id_str, "running", pct, message, retry_count)

    with tracer.start_as_current_span(
        f"job.execute/{job_type}",
        context=parent_ctx,
        kind=SpanKind.CONSUMER,
    ) as span:
        span.set_attribute("job.id", job_id_str)
        span.set_attribute("job.type", job_type)
        span.set_attribute("job.retry_count", retry_count)

        try:
            result: dict[str, Any] = await processor(payload, _publish)

        except Exception as exc:
            new_retry_count = retry_count + 1
            span.record_exception(exc)
            span.set_status(trace.StatusCode.ERROR, str(exc))
            logger.warning(
                "job failed",
                extra={
                    "error": str(exc),
                    "retry_count": new_retry_count,
                    "max_retries": max_retries,
                },
            )

            if new_retry_count < max_retries:
                delay = settings.job_retry_backoff_base ** new_retry_count
                async with session_factory() as session:
                    async with session.begin():
                        await JobRepository(session).update_status(
                            job_id, JobStatus.PENDING,
                            extra={"retry_count": new_retry_count, "error_message": str(exc)},
                        )
                await queue.push_delayed(redis, job_id_str, delay)
                await progress.publish(
                    redis, job_id_str, "retrying",
                    0, f"Retrying in {delay:.0f}s (attempt {new_retry_count}/{max_retries})",
                    retry_count=new_retry_count,
                )
                logger.info("job scheduled for retry", extra={"delay_seconds": delay})
                await metrics.emit_count("JobFailed", dimensions={"JobType": str(job_type)})
            else:
                async with session_factory() as session:
                    async with session.begin():
                        repo = JobRepository(session)
                        audit = AuditRepository(session)
                        await repo.update_status(
                            job_id, JobStatus.DEAD_LETTER,
                            extra={"retry_count": new_retry_count, "error_message": str(exc)},
                        )
                        await audit.log(
                            "job.dead_letter",
                            job_id=job_id,
                            extra_data={"error": str(exc), "retry_count": new_retry_count},
                        )
                await progress.publish(
                    redis, job_id_str, "dead_letter",
                    0, f"Job exhausted after {new_retry_count} attempts: {exc}",
                    retry_count=new_retry_count,
                )
                logger.error("job dead-lettered", extra={"error": str(exc)})
                await metrics.emit_count("JobDeadLettered", dimensions={"JobType": str(job_type)})

            job_id_var.reset(token)
            return

        # ------------------------------------------------------------------ #
        # 3. Persist result                                                    #
        # ------------------------------------------------------------------ #
        async with session_factory() as session:
            async with session.begin():
                repo = JobRepository(session)
                audit = AuditRepository(session)
                await repo.update_status(
                    job_id, JobStatus.COMPLETED,
                    extra={"result": result},
                )
                await audit.log(
                    "job.completed",
                    job_id=job_id,
                    extra_data={"type": job_type, "retry_count": retry_count},
                )

        span.set_status(trace.StatusCode.OK)

    await progress.publish(redis, job_id_str, "completed", 100, "Job completed successfully")
    logger.info("job completed", extra={"type": job_type})
    await metrics.emit_count("JobCompleted", dimensions={"JobType": str(job_type)})
    job_id_var.reset(token)


async def worker_loop(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Any,
) -> None:
    """
    Main worker loop — runs forever as a background asyncio task.

    Each iteration:
      1. Promote delayed jobs that are ready.
      2. Pop one job from the queue.
      3. Spawn an asyncio task to process it (non-blocking).
      4. Sleep briefly before next poll.

    We track in-flight tasks to respect MAX_CONCURRENT_JOBS.
    """
    logger.info("worker loop started")
    in_flight: set[asyncio.Task[None]] = set()
    _metric_tick = 0
    _metric_interval = 120  # emit queue metrics every 120 ticks (~60 s at 0.5 s/tick)

    while True:
        try:
            await queue.promote_delayed(redis)

            if len(in_flight) < MAX_CONCURRENT_JOBS:
                job_id_str = await queue.pop(redis)
                if job_id_str:
                    task = asyncio.create_task(
                        _run_job(job_id_str, session_factory, redis)
                    )
                    in_flight.add(task)
                    task.add_done_callback(in_flight.discard)

            _metric_tick += 1
            if _metric_tick >= _metric_interval:
                _metric_tick = 0
                depth = await queue.queue_length(redis)
                await metrics.emit_gauge("QueueDepth", float(depth))
                await metrics.emit_gauge("InFlightJobs", float(len(in_flight)))

        except asyncio.CancelledError:
            logger.info("worker loop cancelled, waiting for in-flight jobs")
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)
            break
        except Exception as exc:
            logger.error("worker loop error", extra={"error": str(exc)})

        await asyncio.sleep(POLL_INTERVAL)
