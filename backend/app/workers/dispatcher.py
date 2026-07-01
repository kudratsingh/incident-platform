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
from app.repositories.outbox import OutboxRepository
from app.services import retry_policy
from app.workers import (
    async_tasks,
    cpu_processors,
    kafka_producer,
    queue,
    thread_adapters,
)
from app.workers.audit_consumer import AuditConsumer
from app.workers.dependency_resolver import DependencyResolver
from app.workers.event_log_consumer import EventLogConsumer
from app.workers.kafka_consumer import BaseKafkaConsumer
from app.workers.read_model import ReadModelProjector
from app.workers.saga_coordinator import SagaCoordinator
from app.workers.sse_consumer import SseConsumer
from app.workers.triage_consumer import LlmTriageConsumer
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
            user_id = job.user_id
            tenant_id = job.tenant_id
            retry_count = job.retry_count
            max_retries = job.max_retries
            prior_error = job.error_message  # filled when this is a retry

            await repo.update_status(job_id, JobStatus.RUNNING)

    await kafka_producer.publish_job_progress(
        job_id=job_id,
        user_id=user_id,
        tenant_id=tenant_id,
        status="running",
        percent=0,
        message="Job started",
        retry_count=retry_count,
    )
    logger.info("job started", extra={"type": job_type, "retry_count": retry_count})

    # Restore the OTel trace context that was injected at job creation time so
    # this span becomes a child of the original HTTP request span.
    otel_carrier: dict[str, str] = payload.pop("__traceparent", {})
    parent_ctx = extract_context(otel_carrier) if otel_carrier else None

    # ------------------------------------------------------------------ #
    # 2. Execute processor                                                  #
    # ------------------------------------------------------------------ #
    # Resolve the processor. Two things can go wrong here, and both must
    # end in DEAD_LETTER (never leave the job in RUNNING or FAILED-without-retry):
    #
    #   1. The job's `type` string isn't a valid JobType member. This is
    #      the common case for saga compensation jobs, whose type is
    #      `{parent_type}.compensate` (e.g. "csv_upload.compensate") and
    #      is NOT a JobType enum value. Applications register their own
    #      compensation processors in _PROCESSORS keyed by the compensate
    #      string; if they haven't, the job must dead-letter so the saga
    #      status settles instead of hanging in COMPENSATING forever.
    #   2. The string IS a valid JobType but no processor is registered
    #      for it. Same terminal outcome for the same reason.
    #
    # Historical bug: `JobType(job_type)` was evaluated outside the None
    # check, so a `.compensate` string raised ValueError, which propagated
    # into _run_and_release's fire-and-forget task and was silently
    # swallowed — leaving the job stuck in RUNNING and the saga stuck in
    # COMPENSATING.
    processor: Any = None
    try:
        processor = _PROCESSORS.get(JobType(job_type))
    except ValueError:
        # Not a JobType member — could still be a registered compensation
        # type (though currently _PROCESSORS is keyed by JobType only; kept
        # as a future extension point). Fall through to the DEAD_LETTER path.
        processor = None

    if processor is None:
        error = f"No processor for type: {job_type}"
        async with session_factory() as session:
            async with session.begin():
                repo = JobRepository(session)
                audit = AuditRepository(session)
                await repo.update_status(
                    job_id, JobStatus.DEAD_LETTER,
                    extra={"retry_count": retry_count, "error_message": error},
                )
                await audit.log(
                    "job.dead_letter",
                    tenant_id=tenant_id,
                    job_id=job_id,
                    extra_data={"error": error, "reason": "unregistered_type"},
                )
                await OutboxRepository(session).add(
                    tenant_id=tenant_id,
                    topic=settings.kafka_topic_job_dlq,
                    key=f"{tenant_id}:{user_id}",
                    payload={
                        "event": "job.failed",
                        "tenant_id": str(tenant_id),
                        "job_id": job_id_str,
                        "user_id": str(user_id),
                        "job_type": job_type,
                        "error": error,
                        "message": error,
                        "retry_count": retry_count,
                        "dead_lettered": True,
                    },
                )
        logger.error("job dead-lettered — unregistered type", extra={"job_type": job_type})
        await metrics.emit_count("JobDeadLettered", dimensions={"JobType": str(job_type)})
        job_id_var.reset(token)
        return

    async def _publish(pct: int, message: str) -> None:
        await kafka_producer.publish_job_progress(
            job_id=job_id,
            user_id=user_id,
            tenant_id=tenant_id,
            status="running",
            percent=pct,
            message=message,
            retry_count=retry_count,
        )

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

            # Deterministic decision first; LLM only refines it when eligible.
            deterministic_delay = settings.job_retry_backoff_base ** new_retry_count
            llm_dead_lettered = False
            llm_reasoning: str | None = None
            delay = deterministic_delay
            if (
                new_retry_count < max_retries
                and retry_policy.is_enabled()
                and new_retry_count >= settings.llm_retry_policy_min_retry_count
            ):
                # Best-effort consult. Any failure (timeout, schema, network,
                # missing API key) falls back to the deterministic backoff —
                # the worker never blocks waiting on the API.
                try:
                    decision, _usage, _model = await retry_policy.decide_retry(
                        job_type=job_type,
                        error_message=str(exc),
                        retry_count=new_retry_count,
                        max_retries=max_retries,
                        prior_error=prior_error,
                    )
                    llm_reasoning = decision.reasoning
                    if decision.action == "dead_letter_now":
                        llm_dead_lettered = True
                    else:
                        delay = decision.backoff_seconds
                    logger.info(
                        "retry policy decision",
                        extra={
                            "action": decision.action,
                            "backoff_seconds": delay,
                            "deterministic_backoff_seconds": deterministic_delay,
                            "reasoning": decision.reasoning,
                        },
                    )
                except Exception as policy_exc:
                    logger.warning(
                        "retry policy fell back to deterministic",
                        extra={"error": str(policy_exc)},
                    )

            if new_retry_count < max_retries and not llm_dead_lettered:
                async with session_factory() as session:
                    async with session.begin():
                        await JobRepository(session).update_status(
                            job_id, JobStatus.PENDING,
                            extra={"retry_count": new_retry_count, "error_message": str(exc)},
                        )
                        await OutboxRepository(session).add(
                            tenant_id=tenant_id,
                            topic=settings.kafka_topic_job_failed,
                            key=f"{tenant_id}:{user_id}",
                            payload={
                                "event": "job.failed",
                                "tenant_id": str(tenant_id),
                                "job_id": job_id_str,
                                "user_id": str(user_id),
                                "job_type": job_type,
                                "error": str(exc),
                                "message": (
                                    f"Retrying in {delay:.0f}s "
                                    f"(attempt {new_retry_count}/{max_retries})"
                                ),
                                "retry_count": new_retry_count,
                                "dead_lettered": False,
                            },
                        )
                await queue.push_delayed(redis, job_id_str, delay)
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
                        dlq_extra: dict[str, Any] = {
                            "error": str(exc),
                            "retry_count": new_retry_count,
                        }
                        if llm_dead_lettered:
                            dlq_extra["dead_lettered_by"] = "llm_retry_policy"
                            dlq_extra["reasoning"] = llm_reasoning
                        await audit.log(
                            "job.dead_letter",
                            tenant_id=tenant_id,
                            job_id=job_id,
                            extra_data=dlq_extra,
                        )
                        dlq_message = (
                            f"LLM dead-lettered: {llm_reasoning}"
                            if llm_dead_lettered
                            else f"Job exhausted after {new_retry_count} attempts: {exc}"
                        )
                        await OutboxRepository(session).add(
                            tenant_id=tenant_id,
                            topic=settings.kafka_topic_job_dlq,
                            key=f"{tenant_id}:{user_id}",
                            payload={
                                "event": "job.failed",
                                "tenant_id": str(tenant_id),
                                "job_id": job_id_str,
                                "user_id": str(user_id),
                                "job_type": job_type,
                                "error": str(exc),
                                "message": dlq_message,
                                "retry_count": new_retry_count,
                                "dead_lettered": True,
                            },
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
                    tenant_id=tenant_id,
                    job_id=job_id,
                    extra_data={"type": job_type, "retry_count": retry_count},
                )
                await OutboxRepository(session).add(
                    tenant_id=tenant_id,
                    topic=settings.kafka_topic_job_completed,
                    key=f"{tenant_id}:{user_id}",
                    payload={
                        "event": "job.completed",
                        "tenant_id": str(tenant_id),
                        "job_id": job_id_str,
                        "user_id": str(user_id),
                        "job_type": job_type,
                        "result": result,
                        "retry_count": retry_count,
                    },
                )

        span.set_status(trace.StatusCode.OK)

    logger.info("job completed", extra={"type": job_type})
    await metrics.emit_count("JobCompleted", dimensions={"JobType": str(job_type)})
    job_id_var.reset(token)


class JobDispatcherConsumer(BaseKafkaConsumer):
    """
    Consumes `job.submitted` and dispatches each job to `_run_job`.

    Concurrency: an asyncio.Semaphore bounds in-flight jobs to MAX_CONCURRENT_JOBS.
    When the worker is saturated, `handle_message` blocks on acquire, naturally
    creating backpressure against the partition (the consumer stops polling until
    a slot opens — within max_poll_interval_ms, otherwise it's kicked from the group).

    Offset semantics: the base class commits offsets after `handle_message` returns.
    We spawn `_run_job` as a background task and return immediately, so the offset
    advances at dispatch time rather than at job completion. The trade-off:
      * Pro: high throughput, the consumer is never blocked by a long job.
      * Con: a worker crash between commit-and-completion leaves the job in DB
        as RUNNING. Recovery is left to the outbox pattern in a follow-up.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Any,
        max_concurrent: int = MAX_CONCURRENT_JOBS,
    ) -> None:
        settings = get_settings()
        super().__init__(
            topics=[settings.kafka_topic_job_submitted],
            group_id=settings.kafka_consumer_group_worker,
        )
        self.session_factory = session_factory
        self.redis = redis
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.in_flight: set[asyncio.Task[None]] = set()

    async def handle_message(
        self,
        topic: str,
        key: str | None,
        value: dict[str, Any],
        **_kafka_meta: Any,
    ) -> None:
        job_id_str = value.get("job_id") if isinstance(value, dict) else None
        if not job_id_str:
            logger.warning(
                "skipping malformed job.submitted message",
                extra={"topic": topic, "key": key, "value": value},
            )
            return

        # Acquire a slot — blocks (and stops polling) when at capacity.
        await self.semaphore.acquire()
        task = asyncio.create_task(self._run_and_release(job_id_str))
        self.in_flight.add(task)
        task.add_done_callback(self.in_flight.discard)

    async def _run_and_release(self, job_id_str: str) -> None:
        try:
            await _run_job(job_id_str, self.session_factory, self.redis)
        except Exception as exc:
            # Last-resort safety net. `_run_job` is expected to handle its
            # own failures and settle the job in a terminal state; if it
            # somehow escapes with an exception, this task is fire-and-forget
            # (spawned via asyncio.create_task) so the exception would
            # otherwise be silently swallowed and the job would stay in
            # RUNNING forever. Log loudly and mark the job DEAD_LETTER so an
            # admin sees it in the DLQ tab rather than losing it.
            logger.exception(
                "run_job escaped with unhandled exception — force-dead-lettering",
                extra={"job_id": job_id_str, "error": str(exc)},
            )
            try:
                await self._force_dead_letter(job_id_str, str(exc))
            except Exception:
                logger.exception(
                    "force_dead_letter itself failed — job may be stranded",
                    extra={"job_id": job_id_str},
                )
        finally:
            self.semaphore.release()

    async def _force_dead_letter(self, job_id_str: str, error: str) -> None:
        """Best-effort: mark a job DEAD_LETTER when _run_job escapes with an
        unhandled exception. Used only from the _run_and_release safety net."""
        try:
            job_id = uuid.UUID(job_id_str)
        except ValueError:
            return
        async with self.session_factory() as session:
            async with session.begin():
                repo = JobRepository(session)
                job = await repo.get_by_id(job_id)
                if job is None or job.status == JobStatus.DEAD_LETTER:
                    return
                await repo.update_status(
                    job_id,
                    JobStatus.DEAD_LETTER,
                    extra={"error_message": f"Dispatcher escape: {error}"},
                )
                await AuditRepository(session).log(
                    "job.dead_letter",
                    tenant_id=job.tenant_id,
                    job_id=job_id,
                    extra_data={"error": error, "reason": "dispatcher_escape"},
                )

    async def consumer_lag(self) -> int:
        """Sum of (log_end_offset - committed_offset) across all assigned partitions.

        Returns 0 if the consumer hasn't joined the group yet or has no
        assignments — caller treats "unknown" as "not backed up."
        """
        consumer = self._consumer
        if consumer is None:
            return 0
        try:
            assignment = consumer.assignment()
            if not assignment:
                return 0
            end_offsets = await consumer.end_offsets(list(assignment))
            lag = 0
            for tp in assignment:
                committed = await consumer.committed(tp)
                end = end_offsets.get(tp, 0)
                if committed is None:
                    # Never committed — everything in the log is pending.
                    lag += int(end)
                else:
                    lag += max(0, int(end) - int(committed))
            return lag
        except Exception as exc:
            logger.warning("consumer_lag query failed", extra={"error": str(exc)})
            return 0


async def _promote_delayed_loop(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Any,
) -> None:
    """
    Periodically re-queue delayed retry jobs once their backoff has elapsed.

    The re-publish goes through the outbox (not direct Kafka) so the retry
    survives a worker crash between Redis pop and Kafka publish.
    """
    settings = get_settings()
    while True:
        try:
            ready_ids = await queue.pop_ready_delayed(redis)
            for job_id_str in ready_ids:
                try:
                    job_id = uuid.UUID(job_id_str)
                except ValueError:
                    logger.warning("invalid delayed job id", extra={"id": job_id_str})
                    continue

                async with session_factory() as session:
                    async with session.begin():
                        job = await JobRepository(session).get_by_id(job_id)
                        if job is None:
                            logger.warning(
                                "delayed job not found, dropping",
                                extra={"job_id": job_id_str},
                            )
                            continue
                        await OutboxRepository(session).add(
                            tenant_id=job.tenant_id,
                            topic=settings.kafka_topic_job_submitted,
                            key=f"{job.tenant_id}:{job.user_id}",
                            payload={
                                "event": "job.submitted",
                                "tenant_id": str(job.tenant_id),
                                "job_id": str(job.id),
                                "user_id": str(job.user_id),
                                "job_type": job.type,
                                "payload": dict(job.payload or {}),
                                "priority": job.priority,
                                "trace_id": job.trace_id,
                            },
                        )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("promote loop error", extra={"error": str(exc)})

        await asyncio.sleep(POLL_INTERVAL)


OUTBOX_RELAY_INTERVAL = 1.0  # seconds between outbox polls
OUTBOX_RELAY_BATCH = 100


async def _outbox_relay_loop(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """
    Transactional outbox relay.

    Each tick:
      1. Read up to OUTBOX_RELAY_BATCH unpublished rows in one transaction.
      2. For each row, attempt to publish to Kafka.
      3. Mark successfully-published rows in a second transaction.
      4. Rows whose publish failed remain unpublished and are retried next tick.
    """
    while True:
        try:
            async with session_factory() as session:
                async with session.begin():
                    repo = OutboxRepository(session)
                    events = await repo.fetch_unpublished(limit=OUTBOX_RELAY_BATCH)

            if not events:
                await asyncio.sleep(OUTBOX_RELAY_INTERVAL)
                continue

            published_ids: list[uuid.UUID] = []
            failed_ids: list[uuid.UUID] = []
            for event in events:
                try:
                    await kafka_producer.publish_raw(
                        topic=event.topic, key=event.key, payload=event.payload
                    )
                    published_ids.append(event.id)
                except Exception as exc:
                    failed_ids.append(event.id)
                    logger.warning(
                        "outbox publish failed, will retry",
                        extra={
                            "outbox_id": str(event.id),
                            "topic": event.topic,
                            "error": str(exc),
                        },
                    )

            async with session_factory() as session:
                async with session.begin():
                    repo = OutboxRepository(session)
                    await repo.mark_published(published_ids)
                    await repo.increment_attempts(failed_ids)

            if published_ids:
                logger.info(
                    "outbox batch published",
                    extra={"published": len(published_ids), "failed": len(failed_ids)},
                )

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("outbox relay error", extra={"error": str(exc)})

        await asyncio.sleep(OUTBOX_RELAY_INTERVAL)


BACKPRESSURE_LAG_KEY = "kafka:consumer_lag:worker-dispatcher"
BACKPRESSURE_LAG_TTL = 90  # seconds — must exceed metrics loop interval (60s)


async def _digest_loop(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Periodic incident-summary digest worker.

    Runs forever, sleeping `llm_digest_interval_hours` between batches.
    When the feature flag is off, the body short-circuits and we still
    sleep so the loop doesn't busy-wait.
    """
    from app.services import incident_digest

    while True:
        try:
            settings = get_settings()
            interval_seconds = max(60, settings.llm_digest_interval_hours * 3600)
            await asyncio.sleep(interval_seconds)
            if not incident_digest.is_enabled():
                continue
            written = await incident_digest.run_digest_for_all_active_tenants(
                session_factory
            )
            logger.info("digest batch finished", extra={"written": written})
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("digest loop error", extra={"error": str(exc)})


async def _metrics_loop(redis: Any, consumer: JobDispatcherConsumer) -> None:
    """Emit queue/in-flight/consumer-lag gauges every ~60s.

    Lag is also cached in Redis so the API can read it cheaply for the
    backpressure check (no per-request Kafka query).
    """
    while True:
        try:
            await asyncio.sleep(60.0)
            delayed = await queue.delayed_length(redis)
            lag = await consumer.consumer_lag()
            await metrics.emit_gauge("QueueDepth", float(delayed))
            await metrics.emit_gauge("InFlightJobs", float(len(consumer.in_flight)))
            await metrics.emit_gauge("ConsumerLag", float(lag))
            await redis.set(BACKPRESSURE_LAG_KEY, lag, ex=BACKPRESSURE_LAG_TTL)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("metrics loop error", extra={"error": str(exc)})


async def worker_loop(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Any,
) -> None:
    """
    Start the Kafka consumers and the supporting background loops.

    Concurrent tasks that make up the worker:
      1. dispatcher.run()       — consumes `job.submitted`, spawns _run_job.
      2. audit.run()            — consumes lifecycle events, writes audit rows.
      3. sse.run()              — consumes lifecycle events, bridges to Redis pub/sub.
      4. _promote_delayed_loop  — re-publishes delayed retries via the outbox.
      5. _outbox_relay_loop     — publishes outbox rows to Kafka.
      6. _metrics_loop          — emits queue/in-flight gauges to CloudWatch.
      7. _digest_loop           — periodic LLM incident summaries (off by default).

    Cancel signal: cancel all, wait for in-flight jobs, stop all consumers.
    Independent consumer groups — failure in one doesn't affect the others.
    """
    dispatcher = JobDispatcherConsumer(session_factory, redis)
    audit = AuditConsumer(session_factory)
    sse = SseConsumer(redis)
    event_log = EventLogConsumer(session_factory)
    read_model = ReadModelProjector(redis)
    dep_resolver = DependencyResolver(session_factory)
    saga = SagaCoordinator(session_factory)
    triage = LlmTriageConsumer(session_factory)
    consumers: list[BaseKafkaConsumer] = [
        dispatcher,
        audit,
        sse,
        event_log,
        read_model,
        dep_resolver,
        saga,
        triage,
    ]

    started: list[BaseKafkaConsumer] = []
    for c in consumers:
        try:
            await c.start()
            started.append(c)
        except Exception as exc:
            logger.error(
                "kafka consumer failed to start",
                extra={"group_id": c.group_id, "error": str(exc)},
            )

    if dispatcher not in started:
        logger.error("dispatcher consumer not running — worker disabled")
        for c in started:
            await c.stop()
        return

    logger.info(
        "worker loop started", extra={"consumers": [c.group_id for c in started]}
    )
    tasks = [asyncio.create_task(c.run()) for c in started]
    tasks.extend(
        [
            asyncio.create_task(_promote_delayed_loop(session_factory, redis)),
            asyncio.create_task(_outbox_relay_loop(session_factory)),
            asyncio.create_task(_metrics_loop(redis, dispatcher)),
            asyncio.create_task(_digest_loop(session_factory)),
        ]
    )

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("worker loop cancelled, waiting for in-flight jobs")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if dispatcher.in_flight:
            await asyncio.gather(*dispatcher.in_flight, return_exceptions=True)
        raise
    finally:
        for c in started:
            await c.stop()
