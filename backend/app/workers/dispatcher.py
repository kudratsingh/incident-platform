"""
Worker dispatcher: promotes delayed retries, dispatches each job to its processor,
and handles retry backoff and dead-lettering. `worker_loop` hosts the 8 Kafka
consumer groups and the 11 background loops.

Concurrency by type: bulk_api_sync asyncio (concurrent I/O), csv_upload threading
(blocking SDK), doc_analysis + report_gen multiprocessing (CPU-bound, GIL escaped).
"""

import asyncio
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import get_settings
from app.core import metrics
from app.core.circuit_breaker import record_registered_breakers
from app.core.consumer_lag import (
    LAG_SAMPLES_TTL as _LAG_SAMPLES_TTL,
)
from app.core.consumer_lag import (
    LIVE_REFRESHED_GROUP,
    lag_value_ttl_seconds,
    metrics_interval_seconds,
    record_lag_sample,
)
from app.core.consumer_lag import (
    samples_key as _samples_key,
)
from app.core.leader_lock import OUTBOX_RELAY_LOCK_KEY, advisory_leader_lock
from app.core.logging import get_logger, job_id_var, trace_id_var
from app.core.outbox_heartbeat import record_relay_tick
from app.core.tracing import extract_context, get_tracer
from app.models.enums import TERMINAL_JOB_STATUSES, JobStatus, JobType
from app.models.job import Job
from app.models.job_dependency import JobDependency
from app.repositories.audit import AuditRepository
from app.repositories.job import JobRepository
from app.repositories.job_dependency import JobDependencyRepository
from app.repositories.outbox import OutboxRepository
from app.services import alert_rules, retry_policy
from app.utils.dag_pause import find_blocking_pause
from app.utils.post_commit import run_post_commit
from app.workers import (
    async_tasks,
    cpu_processors,
    db_pool_hold,
    db_slow_query,
    dlq_replay_scheduler,
    kafka_producer,
    queue,
    thread_adapters,
)
from app.workers.audit_consumer import AuditConsumer
from app.workers.control_loop_pause import ControlLoopName, loop_is_paused
from app.workers.dependency_resolver import DependencyResolver
from app.workers.event_log_consumer import EventLogConsumer
from app.workers.kafka_consumer import BaseKafkaConsumer, _check_chaos_kill_strict
from app.workers.read_model import ReadModelProjector
from app.workers.saga_coordinator import SagaCoordinator
from app.workers.schema_registry import SchemaValidationError
from app.workers.sse_consumer import SseConsumer
from app.workers.supervisor import worker_tick
from app.workers.triage_consumer import LlmTriageConsumer
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from sqlalchemy import literal, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

logger = get_logger(__name__)
tracer = get_tracer(__name__)

POLL_INTERVAL = 0.5  # seconds between queue checks

# Slower than the retry loops on purpose: only catches children whose promotion
# event has already passed (a DAG pause, or a missed job.completed).
_RESUME_SWEEP_INTERVAL = 10  # seconds
# Bounds *promotable* work only — the SQL below excludes unmet-parent rows (R2-09).
_RESUME_SWEEP_LIMIT = 200

# Re-push delay when a delayed-retry promotion fails. Short: the backoff has already
# elapsed, so this is spacing against a transient error, not a new backoff.
_PROMOTE_RETRY_DELAY_SECONDS = 5.0

# Stale-PENDING backstop: much slower and much older than the retry loop because it
# exists only for the crash windows nothing else covers.
_STALE_PENDING_SWEEP_INTERVAL = 60  # seconds between passes
_STALE_PENDING_AGE_SECONDS = 300  # how long PENDING-without-progress is "stale"
_STALE_PENDING_LIMIT = 100  # PENDING rows examined per pass

# E1-08 / ADR 0011 amendment. A dispatch held back by a paused DAG is pushed onto
# `jobs:delayed`, not dropped; 10s matches the resume sweep's cadence.
_PAUSE_RECHECK_SECONDS = 10.0

# Same for a *scheduled* DLQ replay held by a paused DAG, on its own ZSET and longer:
# re-scheduling leaves `_promote_dlq_replay_loop`'s no-re-enqueue-on-failure policy
# for real failures.
_PAUSED_REPLAY_DEFER_SECONDS = 30

# E1-17 / ADR 0019. Crash-recovery sweep for RUNNING orphans. Slow on purpose: the
# age threshold is `settings.stale_running_threshold_seconds` (900s), so a minute of
# scan latency on top is noise.
_STALE_RUNNING_SWEEP_INTERVAL = 60.0  # seconds between passes
_STALE_RUNNING_SWEEP_LIMIT = 100  # RUNNING rows examined per pass

# WO-R2-28. The lease telling one replica's sweep that another is still executing a
# job: `jobs.heartbeat_at`, renewed every interval and read as live for the TTL. The
# ratio is what matters — six renewals fit one TTL, so a blip, a slow pass or a GC
# pause cannot expire a healthy worker's lease.
_RUNNING_LEASE_RENEW_INTERVAL = 20.0  # seconds between check-ins
_RUNNING_LEASE_TTL_SECONDS = 120.0  # how long a check-in vouches for a job

MAX_CONCURRENT_JOBS = 10  # cap on simultaneously running jobs

# WO-R2-07 / ADR 0021. How long a job may stay RUNNING past the stale-RUNNING
# threshold before the sweep reclaims it despite being one of this process's in-flight
# ids. ADR 0019's unconditional exclusion made a hung local job the one unrecoverable
# state; the grace covers only the deadline breach and the dead-letter write it
# triggers. Sized against the threshold, not the deadline.
_IN_FLIGHT_EXCLUSION_GRACE_SECONDS = 300.0

# Cap on jobs dispatched but not yet finished (running plus waiting for a slot).
# `handle_message` no longer blocks on the semaphore, so without a cap a saturated
# worker would spawn a task per message. Past the cap it raises `DispatchBacklogFull`:
# the offset is not committed and the partition seeks back, without the poll loop
# stopping — `getmany()` keeps being called and the group keeps its member.
_MAX_DISPATCH_BACKLOG = MAX_CONCURRENT_JOBS * 10


class JobExecutionTimeout(Exception):
    """A processor overran `job_execution_timeout_seconds`.

    Deliberately NOT a bare `TimeoutError`: a processor's own `TimeoutError` is an
    ordinary transient failure with retries still owed. See `_execute_processor`.
    """


class DispatchBacklogFull(Exception):
    """Raised by `handle_message` at the dispatch backlog cap: the base consumer
    leaves the offset uncommitted and seeks back for redelivery.
    """

# Strategy map: job type → processor coroutine
_PROCESSORS = {
    JobType.BULK_API_SYNC: async_tasks.process_bulk_api_sync,
    JobType.CSV_UPLOAD: thread_adapters.process_csv_upload,
    JobType.DOC_ANALYSIS: cpu_processors.process_doc_analysis,
    JobType.REPORT_GEN: cpu_processors.process_report_gen,
}

# Terminal-event payloads live in `app/schemas/job_events.py` and are built by
# `JobRepository.update_status` alone (ADR 0001 addendum) — writing the status IS
# emitting the event.


def _execution_timeout_seconds(job_type: str) -> float:
    """The execution deadline for `job_type`, in seconds.

    One knob for every type today; the seam exists so the first per-type deadline
    has somewhere to go that is not a call site.
    """
    return float(get_settings().job_execution_timeout_seconds)


async def _execute_processor(
    processor: Any,
    payload: dict[str, Any],
    publish: Any,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run `processor` under a hard deadline.

    Raises `JobExecutionTimeout` only when `deadline.expired()`; a `TimeoutError` the
    processor raised itself passes through with its retries intact. Work already
    handed to a thread or process pool is not cancelled by this (ADR 0021).
    """
    try:
        async with asyncio.timeout(timeout_seconds) as deadline:
            result: dict[str, Any] = await processor(payload, publish)
            return result
    except TimeoutError:
        if deadline.expired():
            raise JobExecutionTimeout(
                f"job exceeded the {timeout_seconds}s execution deadline"
            ) from None
        raise


async def _run_job(
    job_id_str: str,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Any,
) -> None:
    """Fetch, execute, and finalize a single job. Handles retry / dead-letter."""
    settings = get_settings()
    job_id = uuid.UUID(job_id_str)
    token = job_id_var.set(job_id_str)

    # 1. Load job and atomically claim PENDING -> RUNNING
    held_by: uuid.UUID | None = None
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
            max_attempts = job.max_attempts
            prior_error = job.error_message  # filled when this is a retry
            # E1-04: the status check above is only a cheap pre-filter. The atomic
            # conditional UPDATE (WHERE status='pending') is the gate that lets
            # exactly one delivery win, and must stay in THIS short transaction.
            # E1-08: pause re-checked pre-claim, or a `job.submitted` already in
            # Kafka would still claim RUNNING and run. Held only after the block —
            # `push_delayed` must not run inside the DB transaction.
            held_by = await find_blocking_pause(
                redis, JobDependencyRepository(session), job_id
            )
            if held_by is None:
                claimed = await repo.claim_for_running(job_id)
                if not claimed:
                    logger.info(
                        "job already claimed by another delivery, skipping",
                        extra={"job_id": job_id_str},
                    )
                    job_id_var.reset(token)
                    return

    if held_by is not None:
        # Status stays PENDING; the delayed set re-dispatches once the pause lifts.
        # Dropping it here would strand the job until the stale-PENDING backstop.
        logger.info(
            "execution held (dag paused)",
            extra={"job_id": job_id_str, "paused_by": str(held_by)},
        )
        try:
            await queue.push_delayed(redis, job_id_str, _PAUSE_RECHECK_SECONDS)
        except Exception as exc:
            logger.error(
                "pause re-check re-queue failed — job may strand in PENDING; "
                "backstop sweep will recover",
                extra={"job_id": job_id_str, "error": str(exc)},
            )
        job_id_var.reset(token)
        return

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

    # Restore the trace context injected at creation, so this span parents to the
    # original HTTP request span.
    otel_carrier: dict[str, str] = payload.pop("__traceparent", {})
    parent_ctx = extract_context(otel_carrier) if otel_carrier else None

    # 2. Execute processor.
    # Both resolution failures must end in DEAD_LETTER so the saga settles instead of
    # hanging in COMPENSATING: a type that is not a JobType member (saga
    # `{parent_type}.compensate` jobs), and a valid member with no processor.
    # `JobType(job_type)` outside the guard once raised ValueError into the
    # fire-and-forget task, stranding the job in RUNNING.
    processor: Any = None
    try:
        processor = _PROCESSORS.get(JobType(job_type))
    except ValueError:
        # Not a JobType member — fall through to the DEAD_LETTER path.
        processor = None

    if processor is None:
        error = f"No processor for type: {job_type}"
        async with session_factory() as session:
            async with session.begin():
                repo = JobRepository(session)
                audit = AuditRepository(session)
                # The `job.dlq` outbox row is written by `update_status` in
                # this same transaction — see ADR 0001's addendum.
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

        timeout_seconds = _execution_timeout_seconds(job_type)
        span.set_attribute("job.execution_timeout_seconds", timeout_seconds)

        try:
            result: dict[str, Any] = await _execute_processor(
                processor, payload, _publish, timeout_seconds
            )

        except JobExecutionTimeout as exc:
            # Terminal on the first breach, deliberately NOT a retry: the deadline is
            # a function of the payload, so more attempts would spend more full
            # deadlines to arrive back here. `retry_count` is left alone for the same
            # reason the crash sweep leaves it alone (ADR 0019) — this was not an
            # attempt that failed on its merits.
            span.record_exception(exc)
            span.set_status(trace.StatusCode.ERROR, str(exc))
            error = f"Execution timed out: {exc}"
            async with session_factory() as session:
                async with session.begin():
                    repo = JobRepository(session)
                    audit = AuditRepository(session)
                    # Terminal status and `job.dlq` outbox row in one write via the
                    # single writer (ADR 0001 addendum). A hand-rolled status write
                    # would kill the job in Postgres with no consumer hearing.
                    await repo.update_status(
                        job_id,
                        JobStatus.DEAD_LETTER,
                        extra={
                            "error_message": error,
                            # Badged so the admin DLQ table can tell an overrun
                            # from a throw without an audit join per row (F2-16).
                            "dead_lettered_by": "execution_timeout",
                        },
                        event_message=error,
                    )
                    await audit.log(
                        "job.dead_letter",
                        tenant_id=tenant_id,
                        job_id=job_id,
                        extra_data={
                            "error": str(exc),
                            "reason": "execution_timeout",
                            "timeout_seconds": timeout_seconds,
                            "retry_count": retry_count,
                        },
                    )
            logger.error(
                "job dead-lettered — execution deadline exceeded",
                extra={
                    "job_type": job_type,
                    "timeout_seconds": timeout_seconds,
                    "retry_count": retry_count,
                },
            )
            await metrics.emit_count(
                "JobDeadLettered", dimensions={"JobType": str(job_type)}
            )
            # Distinct from the aggregate above: deadline breaches are a
            # capacity/payload signal, invisible inside the dead-letter count.
            await metrics.emit_count(
                "JobExecutionTimeout", dimensions={"JobType": str(job_type)}
            )
            job_id_var.reset(token)
            return

        except Exception as exc:
            new_retry_count = retry_count + 1
            span.record_exception(exc)
            span.set_status(trace.StatusCode.ERROR, str(exc))
            logger.warning(
                "job failed",
                extra={
                    "error": str(exc),
                    "retry_count": new_retry_count,
                    "max_attempts": max_attempts,
                },
            )

            # Deterministic decision first; LLM only refines it when eligible.
            deterministic_delay = settings.job_retry_backoff_base ** new_retry_count
            llm_dead_lettered = False
            llm_reasoning: str | None = None
            delay = deterministic_delay
            if (
                new_retry_count < max_attempts
                and retry_policy.is_enabled()
                and new_retry_count >= settings.llm_retry_policy_min_retry_count
            ):
                # Best-effort consult: any failure falls back to the deterministic
                # backoff, and the worker never blocks waiting on the API.
                try:
                    decision, _usage, _model = await retry_policy.decide_retry(
                        job_type=job_type,
                        error_message=str(exc),
                        retry_count=new_retry_count,
                        max_attempts=max_attempts,
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

            # `<`, not `<=`, deliberately (WO-R2-172): the ceiling counts RUNS, so
            # the `max_attempts`-th failure has no run left and dead-letters here.
            if new_retry_count < max_attempts and not llm_dead_lettered:
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
                                    f"(attempt {new_retry_count} of {max_attempts})"
                                ),
                                "retry_count": new_retry_count,
                                "dead_lettered": False,
                            },
                        )
                # Guarded like the other `push_delayed` call sites: PENDING is already
                # committed, so a Redis outage leaves a job with no timer for
                # `_requeue_stale_pending_once`. Letting the error escape reached
                # `_run_and_release`'s net, which dead-lettered a job with retries left.
                try:
                    await queue.push_delayed(redis, job_id_str, delay)
                except Exception as push_exc:
                    logger.error(
                        "retry re-queue failed — job left PENDING; "
                        "stale-PENDING backstop will re-publish it",
                        extra={"job_id": job_id_str, "error": str(push_exc)},
                    )
                logger.info("job scheduled for retry", extra={"delay_seconds": delay})
                await metrics.emit_count("JobFailed", dimensions={"JobType": str(job_type)})
            else:
                async with session_factory() as session:
                    async with session.begin():
                        repo = JobRepository(session)
                        audit = AuditRepository(session)
                        job_extra: dict[str, Any] = {
                            "retry_count": new_retry_count,
                            "error_message": str(exc),
                        }
                        if llm_dead_lettered:
                            # On the row, not only in audit extra_data: the DLQ
                            # table badges per row and cannot afford a join
                            # (F2-16). Unset otherwise — exhausted retries are
                            # the default and claim no attribution.
                            job_extra["dead_lettered_by"] = "llm_retry_policy"
                        # `message` is the only DLQ-event field a call site still
                        # colours; `update_status` derives the rest from the row
                        # and writes the `job.dlq` outbox row here.
                        dlq_message = (
                            f"LLM dead-lettered: {llm_reasoning}"
                            if llm_dead_lettered
                            else f"Job exhausted after {new_retry_count} attempts: {exc}"
                        )
                        await repo.update_status(
                            job_id,
                            JobStatus.DEAD_LETTER,
                            extra=job_extra,
                            event_message=dlq_message,
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
                logger.error("job dead-lettered", extra={"error": str(exc)})
                await metrics.emit_count("JobDeadLettered", dimensions={"JobType": str(job_type)})

            job_id_var.reset(token)
            return

        # 3. Persist result
        async with session_factory() as session:
            async with session.begin():
                repo = JobRepository(session)
                audit = AuditRepository(session)
                # `update_status` writes the `job.completed` outbox row in this
                # transaction; the resolver and the saga coordinator key off it.
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

        span.set_status(trace.StatusCode.OK)

    logger.info("job completed", extra={"type": job_type})
    await metrics.emit_count("JobCompleted", dimensions={"JobType": str(job_type)})
    job_id_var.reset(token)


class JobDispatcherConsumer(BaseKafkaConsumer):
    """
    Consumes `job.submitted` and dispatches each job to `_run_job`.

    The concurrency slot is taken inside the spawned task, never in `handle_message`
    (WO-R2-07, ADR 0021): awaiting it on the poll loop stopped `getmany()` and evicted
    the worker from the group. Backpressure is `_MAX_DISPATCH_BACKLOG` plus
    `job_execution_timeout_seconds`, which both keep the loop polling. Offsets commit
    at dispatch, so a crash leaves RUNNING rows for `_stale_running_sweep_loop` (ADR
    0019, which reads `in_flight_job_ids`) and PENDING rows for the stale-PENDING one.
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
        # Job ids this process is executing; `_sweep_stale_running_once` reads it
        # to exclude live work from crash recovery (E1-17). Separate from
        # `in_flight`, which holds Task objects for shutdown draining.
        self.in_flight_job_ids: set[str] = set()

    async def handle_message(
        self,
        topic: str,
        key: str | None,
        value: dict[str, Any],
        **_kafka_meta: Any,
    ) -> None:
        """Hand one submitted job to a background task and return at once, so
        the poll loop keeps polling however busy the worker is."""
        job_id_str = value.get("job_id") if isinstance(value, dict) else None
        if not job_id_str:
            logger.warning(
                "skipping malformed job.submitted message",
                extra={"topic": topic, "key": key, "value": value},
            )
            return

        # Checked, never *awaited*: this method runs on the poll loop. Raising
        # leaves the offset uncommitted and seeks the partition back (see
        # `BaseKafkaConsumer._process_one`) while `getmany()` keeps being called.
        if len(self.in_flight) >= _MAX_DISPATCH_BACKLOG:
            logger.warning(
                "dispatch backlog full — message not accepted, will be redelivered",
                extra={
                    "job_id": job_id_str,
                    "backlog": len(self.in_flight),
                    "limit": _MAX_DISPATCH_BACKLOG,
                },
            )
            await metrics.emit_count("JobDispatchRejected")
            raise DispatchBacklogFull(
                f"dispatch backlog at capacity ({_MAX_DISPATCH_BACKLOG})"
            )

        # Claim the id BEFORE spawning: the gap before the coroutine's first step
        # is one the sweep could read as an orphan. Dropped only in the `finally`
        # of `_run_and_release`.
        self.in_flight_job_ids.add(job_id_str)
        task = asyncio.create_task(self._run_and_release(job_id_str))
        self.in_flight.add(task)
        task.add_done_callback(self.in_flight.discard)

    async def _run_and_release(self, job_id_str: str) -> None:
        """Wait for a concurrency slot, run the job, and always give the slot
        and the in-flight claim back."""
        # Slot taken HERE, inside the task: waiting for capacity is background
        # work, not something the consumer does instead of polling.
        try:
            await self.semaphore.acquire()
        except BaseException:
            # Cancelled while queued for a slot (reachable on an orderly stop).
            # Drop the claim: an id in `in_flight_job_ids` with no task behind it
            # makes the sweep skip a row nobody is executing.
            self.in_flight_job_ids.discard(job_id_str)
            raise

        try:
            await _run_job(job_id_str, self.session_factory, self.redis)
        except Exception as exc:
            # Last-resort safety net: this task is fire-and-forget, so an escape
            # from `_run_job` would be silently swallowed and leave the job in
            # RUNNING forever. Log loudly and dead-letter so it lands on the DLQ tab.
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
            self.in_flight_job_ids.discard(job_id_str)

    async def _force_dead_letter(self, job_id_str: str, error: str) -> None:
        """Best-effort DEAD_LETTER when `_run_job` escapes — the `_run_and_release` net.

        The status write and its `job.dlq` outbox row are one write inside
        `update_status` (ADR 0001 addendum); without the event nothing downstream
        hears, so the saga, read model, triage and SSE stream all stall.
        """
        try:
            job_id = uuid.UUID(job_id_str)
        except ValueError:
            return
        async with self.session_factory() as session:
            async with session.begin():
                repo = JobRepository(session)
                job = await repo.get_by_id(job_id)
                # Any terminal state, not just DEAD_LETTER: `_run_job` can settle a
                # job COMPLETED and *then* escape, and overwriting it now also mints
                # a `job.dlq` event — a broadcast lie rather than a row-level one.
                if job is None or job.status in TERMINAL_JOB_STATUSES:
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

    async def consumer_lag(self) -> int | None:
        """Sum of (log_end_offset - committed_offset) across all assigned partitions.

        Returns `None`, never 0, for genuinely-unknown states (not started, no
        assignment, Kafka query failed): a fabricated 0 reads as healthy. `None`
        propagates — `_metrics_loop` skips the Redis cache write and the gauge, and
        `check_backpressure` fails open on the absent entry.
        """
        consumer = self._consumer
        if consumer is None:
            return None
        try:
            assignment = consumer.assignment()
            if not assignment:
                return None
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
            return None


def _job_submitted_payload(job: Job) -> dict[str, Any]:
    """The canonical `job.submitted` outbox payload for a job row.

    Shared by every re-publish path (delayed-retry promotion, stale-PENDING
    backstop, resume sweep) so a backstop dispatch is byte-identical to a normal
    one — the resume sweep kept its own inline copy until WO-R2-116.
    """
    return {
        "event": "job.submitted",
        "tenant_id": str(job.tenant_id),
        "job_id": str(job.id),
        "user_id": str(job.user_id),
        "job_type": job.type,
        "payload": dict(job.payload or {}),
        "priority": job.priority,
        "trace_id": job.trace_id,
    }


async def _promote_delayed_once(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Any,
) -> None:
    """One pass of delayed-retry promotion: pop the due entries and re-publish each
    through the outbox.

    `pop_ready_delayed` is destructive, so each item gets its own try/except (E1-03)
    and a failure is pushed back onto `jobs:delayed` — unlike `_promote_dlq_replay_loop`,
    a lost retry leaves no audit trail. The re-push runs outside the dead session, and
    a paused DAG takes the same route (E1-08, ADR 0011 amendment).
    """
    settings = get_settings()
    ready_ids = await queue.pop_ready_delayed(redis)
    for job_id_str in ready_ids:
        try:
            job_id = uuid.UUID(job_id_str)
        except ValueError:
            logger.warning("invalid delayed job id", extra={"id": job_id_str})
            continue

        held_by: uuid.UUID | None = None
        try:
            async with session_factory() as session:
                async with session.begin():
                    job = await JobRepository(session).get_by_id(job_id)
                    if job is None:
                        logger.warning(
                            "delayed job not found, dropping",
                            extra={"job_id": job_id_str},
                        )
                        continue
                    # E1-08: a retry is a new dispatch, so the pause holds it.
                    # Probed after the row exists (a deleted job still drops
                    # above) and before the outbox add, the actual dispatch.
                    held_by = await find_blocking_pause(
                        redis, JobDependencyRepository(session), job_id
                    )
                    if held_by is None:
                        await OutboxRepository(session).add(
                            tenant_id=job.tenant_id,
                            topic=settings.kafka_topic_job_submitted,
                            key=f"{job.tenant_id}:{job.user_id}",
                            payload=_job_submitted_payload(job),
                        )
        except Exception as exc:
            logger.error(
                "delayed promotion failed, re-queueing job",
                extra={"job_id": job_id_str, "error": str(exc)},
            )
            try:
                await queue.push_delayed(
                    redis, job_id_str, _PROMOTE_RETRY_DELAY_SECONDS
                )
            except Exception as push_exc:
                logger.error(
                    "delayed re-queue failed — job may strand in PENDING; "
                    "backstop sweep will recover",
                    extra={"job_id": job_id_str, "error": str(push_exc)},
                )
            continue

        if held_by is not None:
            # Held, not dropped: the pop already ZREM'd this id, so dropping it
            # loses the retry for good. Re-push outside the transaction and let
            # the next pass re-evaluate the pause.
            logger.info(
                "delayed retry held (dag paused)",
                extra={"job_id": job_id_str, "paused_by": str(held_by)},
            )
            try:
                await queue.push_delayed(redis, job_id_str, _PAUSE_RECHECK_SECONDS)
            except Exception as push_exc:
                logger.error(
                    "paused retry re-queue failed — job may strand in PENDING; "
                    "backstop sweep will recover",
                    extra={"job_id": job_id_str, "error": str(push_exc)},
                )


async def _promote_delayed_loop(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Any,
) -> None:
    """Re-queue delayed retry jobs once their backoff has elapsed.

    Re-publishes through the outbox, not direct Kafka, so the retry survives a crash
    between the Redis pop and the Kafka publish. Per-item failures are handled inside
    `_promote_delayed_once`.
    """
    while True:
        # Liveness for the deep health check: this loop's silence is the closest
        # thing the worker has to "the loops are wedged" (`workers/supervisor.py`).
        worker_tick()
        try:
            # AFTER worker_tick(), never before: skipping the heartbeat during a
            # single-loop pause would report the whole worker wedged
            # (`workers/control_loop_pause.py`).
            if not await loop_is_paused(ControlLoopName.DELAYED_RETRY_PROMOTE):
                await _promote_delayed_once(session_factory, redis)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("promote loop error", extra={"error": str(exc)})

        await asyncio.sleep(POLL_INTERVAL)


# Cursor a resume-sweep pass hands to the next one: the (created_at, id) of
# the last row it examined, or None to start again from the oldest.
_ResumeCursor = tuple[Any, uuid.UUID]


def _promotable_waiting_stmt(cursor: _ResumeCursor | None) -> Any:
    """WAITING jobs with no unmet parent, oldest first, after `cursor`.

    The eligibility test lives in SQL, not the Python loop — the whole R2-09 fix:
    `status == WAITING LIMIT 200` let permanently-blocked children (DEAD_LETTER or
    CANCELLED parent) consume every slot forever, platform-wide. `ORDER BY created_at,
    id` plus the rotating cursor covers what the predicate cannot see, a child held
    back in Python by a DAG pause in Redis; `created_at` alone is not unique.
    """
    parent = aliased(Job)
    has_unmet_parent = (
        select(literal(1))
        .select_from(JobDependency)
        .join(parent, parent.id == JobDependency.depends_on_job_id)
        .where(
            JobDependency.job_id == Job.id,
            parent.status != JobStatus.COMPLETED,
        )
        .correlate(Job)
        .exists()
    )
    stmt = (
        select(Job)
        .where(Job.status == JobStatus.WAITING, ~has_unmet_parent)
        .order_by(Job.created_at, Job.id)
        .limit(_RESUME_SWEEP_LIMIT)
    )
    if cursor is not None:
        # Bind each half against its own column type: `Job.id` is a UUID
        # TypeDecorator, and an untyped literal reaches the driver as a raw
        # uuid.UUID that SQLite cannot bind.
        stmt = stmt.where(
            tuple_(Job.created_at, Job.id)
            > tuple_(
                literal(cursor[0], Job.created_at.type),
                literal(cursor[1], Job.id.type),
            )
        )
    return stmt


async def _resume_unblocked_waiting_once(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Any,
    cursor: _ResumeCursor | None = None,
) -> _ResumeCursor | None:
    """One pass of the resume sweep. Returns the cursor for the next pass.

    A short page means the tail was reached, so the cursor resets to None and the
    next pass restarts from the oldest row; a full page hands back its last row.
    """
    settings = get_settings()
    examined = 0
    # Read the cursor *inside* the transaction: after the commit these instances are
    # expired, and an attribute touch emits a lazy refresh on a closed session.
    next_cursor: _ResumeCursor | None = None
    async with session_factory() as session:
        async with session.begin():
            dep_repo = JobDependencyRepository(session)
            job_repo = JobRepository(session)
            outbox_repo = OutboxRepository(session)

            rows = list(
                (
                    await session.execute(_promotable_waiting_stmt(cursor))
                ).scalars()
            )
            examined = len(rows)
            if rows:
                next_cursor = (rows[-1].created_at, rows[-1].id)

            for child in rows:
                # The DAG pause lives in Redis, so it stays a per-row check. A
                # paused child still counts against this pass's page, which is
                # exactly what the cursor exists to survive.
                if (
                    await find_blocking_pause(redis, dep_repo, child.id)
                    is not None
                ):
                    continue

                # E1-04: CAS the promotion — the DependencyResolver or a concurrent
                # pass may win it first, and the loser must skip the outbox add too
                # or it still mints a duplicate job.submitted.
                if not await job_repo.promote_waiting_to_pending(child.id):
                    continue
                # WO-R2-116: the shared builder, not a fourth hand-assembled copy
                # of the same keys.
                await outbox_repo.add(
                    tenant_id=child.tenant_id,
                    topic=settings.kafka_topic_job_submitted,
                    key=f"{child.tenant_id}:{child.user_id}",
                    payload=_job_submitted_payload(child),
                )
                logger.info(
                    "resume sweep promoted child",
                    extra={"child_id": str(child.id)},
                )

    # A short page means the tail was reached: rotate back to the start.
    if examined < _RESUME_SWEEP_LIMIT:
        return None
    return next_cursor


async def _resume_unblocked_waiting_loop(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Any,
) -> None:
    """Promote WAITING jobs whose parents are done and whose DAG is no longer paused.

    The DependencyResolver only reacts to `job.completed`, so without this a child held
    across a pause stays WAITING forever once that event is consumed; it also backstops
    missed promotions. Cross-tenant, and deliberately NOT leader-gated (ADR 0020) — the
    CAS in `promote_waiting_to_pending` makes concurrent sweeps wasted scans, not
    duplicate events. The cursor is a per-replica fairness hint, not correctness.
    """
    cursor: _ResumeCursor | None = None
    while True:
        try:
            # The cursor is deliberately NOT reset while paused: it is a
            # fairness hint, and a pause is not a failure to re-scan from.
            if not await loop_is_paused(ControlLoopName.RESUME_UNBLOCKED_WAITING):
                cursor = await _resume_unblocked_waiting_once(
                    session_factory, redis, cursor
                )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            # Reset on error: the failing pass may have advanced past rows it
            # never examined, and re-scanning is cheap next to stranding them.
            cursor = None
            logger.error("resume sweep error", extra={"error": str(exc)})

        await asyncio.sleep(_RESUME_SWEEP_INTERVAL)


async def _requeue_stale_pending_once(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Any,
) -> None:
    """One pass of the stale-PENDING backstop: re-publish PENDING jobs that nothing
    is going to pick up.

    Covers the two crash windows `_promote_delayed_once` cannot (E1-03): a death
    between the destructive Lua pop and the outbox commit, and one between the retry
    commit and `queue.push_delayed`. The `jobs:delayed` ZSCORE check makes it safe — a
    hit means the job is legitimately waiting out a backoff. Ungated while the relay
    is leader-gated (ADR 0020) because `claim_for_running` (WO-P4-03) is stronger: it
    also covers the false positive no gate can see. Cross-tenant by design.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=_STALE_PENDING_AGE_SECONDS)
    async with session_factory() as session:
        async with session.begin():
            outbox_repo = OutboxRepository(session)
            rows = (
                await session.execute(
                    select(Job)
                    .where(
                        Job.status == JobStatus.PENDING,
                        Job.updated_at < cutoff,
                        # WO-R2-28: and not already re-published inside this
                        # window. Without it a lagging dispatcher got the same
                        # job re-published every 60s. `IS NULL` keeps never-swept
                        # and pre-column rows eligible on the first pass.
                        or_(
                            Job.requeued_at.is_(None),
                            Job.requeued_at < cutoff,
                        ),
                    )
                    .limit(_STALE_PENDING_LIMIT)
                )
            ).scalars()

            for job in rows:
                # `updated_at`, not `created_at`: update_status(PENDING) touches
                # it, so the age measured here is time-since-last-progress.
                if await redis.zscore(queue.DELAYED_KEY, str(job.id)) is not None:
                    continue
                await outbox_repo.add(
                    tenant_id=job.tenant_id,
                    topic=settings.kafka_topic_job_submitted,
                    key=f"{job.tenant_id}:{job.user_id}",
                    payload=_job_submitted_payload(job),
                )
                # Stamped in the SAME transaction as the outbox insert — the whole
                # de-duplication guarantee. `updated_at` is pinned to its own value
                # so `onupdate` does not fire: re-publishing is not progress, and
                # recording it as progress would reset the operator-visible age.
                await session.execute(
                    update(Job)
                    .where(Job.id == job.id)
                    .values(requeued_at=now, updated_at=Job.updated_at)
                )
                logger.info(
                    "stale PENDING re-published",
                    extra={
                        "job_id": str(job.id),
                        "tenant_id": str(job.tenant_id),
                    },
                )


async def _requeue_stale_pending_loop(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Any,
) -> None:
    """Backstop sweep for jobs stranded in PENDING — see
    `_requeue_stale_pending_once` for what it recovers and why the
    `jobs:delayed` guard is load-bearing."""
    while True:
        try:
            if not await loop_is_paused(ControlLoopName.STALE_PENDING_BACKSTOP):
                await _requeue_stale_pending_once(session_factory, redis)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("stale pending sweep error", extra={"error": str(exc)})

        await asyncio.sleep(_STALE_PENDING_SWEEP_INTERVAL)


async def _sweep_stale_running_once(
    session_factory: async_sessionmaker[AsyncSession],
    dispatcher: JobDispatcherConsumer,
    threshold_seconds: int,
) -> int:
    """One pass of the stale-RUNNING crash recovery sweep (E1-17, ADR 0019).

    Offsets commit at dispatch, so a hard crash leaves RUNNING rows nothing will
    redeliver. Recovery is DEAD_LETTER, never re-publish: the job may have run an
    arbitrary prefix of its side effects. Three load-bearing exclusions — the
    cross-replica lease `jobs.heartbeat_at` (WO-R2-28; NULL reads as stale),
    `dispatcher.in_flight_job_ids` for only `_IN_FLIGHT_EXCLUSION_GRACE_SECONDS` past
    the threshold (WO-R2-07), and an age cutoff in SQL because SQLite hands back naive
    datetimes. Each survivor settles in its OWN transaction (E1-03), with the write a
    compare-and-set (`guard=`) against what the scan saw (ADR 0023). Returns the
    number of jobs dead-lettered.
    """
    now_scan = datetime.now(UTC)
    cutoff = now_scan - timedelta(seconds=threshold_seconds)
    lease_cutoff = now_scan - timedelta(seconds=_RUNNING_LEASE_TTL_SECONDS)

    # Scan in its own read-only session; the recoveries below each open their own.
    # `started_at IS NOT NULL` skips legacy or hand-seeded rows rather than
    # crashing the loop on a NULL comparison.
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    Job.id,
                    Job.tenant_id,
                    Job.user_id,
                    Job.type,
                    Job.retry_count,
                    Job.max_attempts,
                    Job.payload,
                    Job.trace_id,
                    Job.started_at,
                    Job.heartbeat_at,
                )
                .where(
                    Job.status == JobStatus.RUNNING,
                    Job.started_at.is_not(None),
                    Job.started_at < cutoff,
                    # The cross-replica exclusion (WO-R2-28), in SQL for the same
                    # reason the age cutoff is: in Python it would spend the page
                    # on jobs that are plainly alive.
                    or_(
                        Job.heartbeat_at.is_(None),
                        Job.heartbeat_at < lease_cutoff,
                    ),
                )
                .limit(_STALE_RUNNING_SWEEP_LIMIT)
            )
        ).all()

    now = datetime.now(UTC)
    recovered = 0
    for row in rows:
        job_id_str = str(row.id)

        # Naive on SQLite, aware on Postgres: normalise before subtracting so the
        # observability fields below can't raise. The decision was made in SQL.
        started_at = row.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        stale_seconds = (now - started_at).total_seconds()

        # The in-flight exclusion is no longer permanent (WO-R2-07, amending ADR 0019
        # §3): a job of ours this far past the threshold is one whose own
        # `job_execution_timeout_seconds` should have fired, so it is stuck, not slow.
        # Inside the grace it still holds, keeping the sweep off a job whose
        # dead-letter write is in flight.
        in_flight = job_id_str in dispatcher.in_flight_job_ids
        if in_flight and stale_seconds < (
            threshold_seconds + _IN_FLIGHT_EXCLUSION_GRACE_SECONDS
        ):
            continue

        error = "worker crash recovery: job exceeded stale-RUNNING threshold"
        if in_flight:
            error = (
                "stuck job recovery: still RUNNING long past its execution "
                "deadline while held by this worker"
            )
        try:
            async with session_factory() as session:
                async with session.begin():
                    repo = JobRepository(session)
                    # retry_count is deliberately NOT touched: a crash recovery is
                    # not a replay, and zeroing it erases the attempt history the
                    # DLQ tab reasons about. `update_status` writes the `job.dlq`
                    # row here with the full `DLQ_EVENT_KEYS` set. `guard` makes it
                    # a compare-and-set against what the scan saw; between the two
                    # transactions the lease may have been renewed or the job
                    # replayed, and only one statement closes that gap.
                    settled = await repo.update_status(
                        row.id,
                        JobStatus.DEAD_LETTER,
                        extra={"error_message": error},
                        guard=(
                            Job.status == JobStatus.RUNNING,
                            Job.started_at == row.started_at,
                            Job.heartbeat_at.is_(None)
                            if row.heartbeat_at is None
                            else Job.heartbeat_at == row.heartbeat_at,
                        ),
                    )
                    if settled is None:
                        # Refused: the row moved under us, so someone else owns
                        # its outcome. Leave it for the next pass's fresh scan.
                        logger.info(
                            "stale RUNNING recovery refused — row changed "
                            "under the sweep",
                            extra={
                                "job_id": job_id_str,
                                "observed_started_at": started_at.isoformat(),
                            },
                        )
                        continue
                    await AuditRepository(session).log(
                        "job.dead_letter",
                        tenant_id=row.tenant_id,
                        job_id=row.id,
                        extra_data={
                            "error": error,
                            # Distinguished so replay tooling and triage can tell
                            # a crash orphan (nobody was running it) from a local
                            # job that outlived its deadline.
                            "reason": (
                                "stuck_local_job"
                                if in_flight
                                else "worker_crash_recovery"
                            ),
                            "stale_seconds": stale_seconds,
                            "started_at": started_at.isoformat(),
                        },
                    )
        except Exception as exc:
            # Per-job isolation: log and move to the next orphan. The row
            # stays RUNNING and the next pass retries it.
            logger.error(
                "stale RUNNING recovery failed",
                extra={"job_id": job_id_str, "error": str(exc)},
            )
            continue

        recovered += 1
        logger.error(
            "stale RUNNING job dead-lettered"
            + (
                " (stuck local job past its deadline)"
                if in_flight
                else " (worker crash recovery)"
            ),
            extra={
                "job_id": job_id_str,
                "tenant_id": str(row.tenant_id),
                "job_type": row.type,
                "stale_seconds": stale_seconds,
                "held_by_this_worker": in_flight,
            },
        )
        await metrics.emit_count(
            "JobDeadLettered", dimensions={"JobType": str(row.type)}
        )

    return recovered


async def _renew_running_leases_once(
    session_factory: async_sessionmaker[AsyncSession],
    dispatcher: JobDispatcherConsumer,
    threshold_seconds: int,
) -> int:
    """One check-in on behalf of the jobs this process is executing (WO-R2-28).

    The write side of the lease `_sweep_stale_running_once` reads: it replaces "is this
    job in *my* in-flight set?" with "has *anyone* checked in lately?", which every
    replica can answer. The id set is snapshotted before awaiting because the same
    event loop mutates it. Renewal stops past the bound the in-flight exclusion uses,
    so a hung worker cannot defend its own stuck job forever. Returns leases renewed.
    """
    job_ids: list[uuid.UUID] = []
    for job_id_str in list(dispatcher.in_flight_job_ids):
        try:
            job_ids.append(uuid.UUID(job_id_str))
        except ValueError:
            # A malformed id never reached a real row, so it has no lease.
            continue
    if not job_ids:
        return 0

    async with session_factory() as session:
        async with session.begin():
            return await JobRepository(session).renew_running_leases(
                job_ids,
                max_age_seconds=(
                    threshold_seconds + _IN_FLIGHT_EXCLUSION_GRACE_SECONDS
                ),
            )


async def _renew_running_leases_loop(
    session_factory: async_sessionmaker[AsyncSession],
    dispatcher: JobDispatcherConsumer,
) -> None:
    """Keep this worker's RUNNING jobs vouched for — see `_renew_running_leases_once`.

    Log and keep turning: the TTL spans six intervals, and a sustained failure only
    degrades the sweep back to the age threshold plus the local in-flight set.
    """
    settings = get_settings()
    while True:
        try:
            if not await loop_is_paused(ControlLoopName.LEASE_RENEWAL):
                await _renew_running_leases_once(
                    session_factory,
                    dispatcher,
                    settings.stale_running_threshold_seconds,
                )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("lease renewal error", extra={"error": str(exc)})

        await asyncio.sleep(_RUNNING_LEASE_RENEW_INTERVAL)


async def _stale_running_sweep_loop(
    session_factory: async_sessionmaker[AsyncSession],
    dispatcher: JobDispatcherConsumer,
) -> None:
    """Crash-recovery sweep for jobs stranded in RUNNING — see
    `_sweep_stale_running_once` (ADR 0019).

    Hard crashes only: an orderly stop settles its own jobs via `worker_loop`'s
    CancelledError path, which is why the threshold is generous, not responsive.
    """
    settings = get_settings()
    while True:
        try:
            if not await loop_is_paused(ControlLoopName.STALE_RUNNING_SWEEP):
                await _sweep_stale_running_once(
                    session_factory,
                    dispatcher,
                    settings.stale_running_threshold_seconds,
                )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("stale running sweep error", extra={"error": str(exc)})

        await asyncio.sleep(_STALE_RUNNING_SWEEP_INTERVAL)


async def _promote_dlq_replay_once(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Any,
) -> None:
    """One pass of scheduled-DLQ-replay promotion: claim the due entries and fire
    each one.

    Each entry hits `JobService.replay_job`, so the execution is indistinguishable
    from an immediate replay; a paused DAG re-schedules instead of firing (E1-08).
    Claim/ack, not pop (R2-21): `claim_ready` moves due members into
    `jobs:dlq_replay_inflight` and every observable outcome acks, so only a worker
    killed mid-replay leaves a claim for a later tick. Failure still does not
    re-enqueue — auto-retrying would mask "job was deleted".
    """
    # Local imports keep the module-level graph flat.
    from app.repositories.job_dependency import JobDependencyRepository
    from app.services.job import JobService

    ready = await dlq_replay_scheduler.claim_ready(redis)
    for tenant_id, principal_id, job_id in ready:
        try:
            held_by: uuid.UUID | None = None
            async with session_factory() as session:
                async with session.begin():
                    # E1-08: probe the pause BEFORE the replay. This loop
                    # does not re-enqueue a failure, so letting `replay_job`
                    # refuse would silently discard the scheduled remediation.
                    held_by = await find_blocking_pause(
                        redis, JobDependencyRepository(session), job_id
                    )
                    if held_by is None:
                        service = JobService(
                            JobRepository(session),
                            AuditRepository(session),
                            OutboxRepository(session),
                            redis,
                            dep_repo=JobDependencyRepository(session),
                        )
                        # Scheduled replays only come from SA callers
                        # (Tier-1 tools) today; a human path would need
                        # principal_type in the ZSET member.
                        await service.replay_job(
                            job_id=job_id,
                            tenant_id=tenant_id,
                            principal_type="service_account",
                            principal_id=principal_id,
                        )
                # This loop owns its own transaction boundary, so it drains the
                # post-commit queue itself — `get_db` never runs here (R2-23).
                # After the commit and before the ack; `run_post_commit` cannot
                # raise, so it cannot turn a committed replay into a "fire failed".
                await run_post_commit(session)
            if held_by is not None:
                await dlq_replay_scheduler.schedule_replay(
                    redis,
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    job_id=job_id,
                    delay_seconds=_PAUSED_REPLAY_DEFER_SECONDS,
                )
                logger.info(
                    "dlq replay deferred (dag paused)",
                    extra={
                        "job_id": str(job_id),
                        "tenant_id": str(tenant_id),
                        "paused_by": str(held_by),
                        "delay_seconds": _PAUSED_REPLAY_DEFER_SECONDS,
                    },
                )
            else:
                logger.info(
                    "dlq replay scheduled fired",
                    extra={
                        "job_id": str(job_id),
                        "tenant_id": str(tenant_id),
                    },
                )
        except Exception as exc:
            logger.error(
                "dlq replay scheduled fire failed",
                extra={
                    "job_id": str(job_id),
                    "tenant_id": str(tenant_id),
                    "error": str(exc),
                },
            )

        # Reached on success, on a paused-DAG deferral (the entry is re-armed on
        # the scheduled set, so holding the claim too would replay it twice) and
        # on a logged failure. Skipped only when the worker dies mid-item.
        await dlq_replay_scheduler.ack_replay(
            redis,
            tenant_id=tenant_id,
            principal_id=principal_id,
            job_id=job_id,
        )


async def _promote_dlq_replay_loop(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Any,
) -> None:
    """Fire operator-scheduled DLQ replays whose delay window has elapsed.

    Drains `jobs:dlq_replay_delayed` (Tier-1 `replay_dlq_by_ids` /
    `replay_dlq_by_category`), not the retry-cycle `jobs:delayed`. Per-pass
    semantics in `_promote_dlq_replay_once`.
    """
    while True:
        try:
            if not await loop_is_paused(ControlLoopName.DLQ_REPLAY_PROMOTE):
                await _promote_dlq_replay_once(session_factory, redis)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error(
                "dlq replay promote loop error", extra={"error": str(exc)}
            )

        await asyncio.sleep(POLL_INTERVAL)


OUTBOX_RELAY_INTERVAL = 1.0  # seconds between outbox polls
OUTBOX_RELAY_BATCH = 100

#: How often the relay reports queue health — the tick runs once a second and
#: CloudWatch does not need that. Only the leader emits, so it stays one series.
_OUTBOX_GAUGE_INTERVAL = 60.0
_last_outbox_gauge_at: float = 0.0


async def _emit_outbox_gauges(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Publish outbox depth + oldest-row age, at most once a minute.

    `QueueDepth` measures the Redis delayed set, which a relay stall leaves green,
    so these two are the only signal that notices — and their failure must never
    take the tick down with it.
    """
    global _last_outbox_gauge_at
    now = time.monotonic()
    if now - _last_outbox_gauge_at < _OUTBOX_GAUGE_INTERVAL:
        return
    _last_outbox_gauge_at = now
    try:
        async with session_factory() as session:
            async with session.begin():
                depth, oldest_age = await OutboxRepository(session).unpublished_stats()
        await metrics.emit_gauge("OutboxUnpublishedDepth", float(depth))
        await metrics.emit_gauge(
            "OutboxOldestUnpublishedAgeSeconds", oldest_age, unit="Seconds"
        )
    except Exception as exc:
        logger.warning("outbox gauge emission failed", extra={"error": str(exc)})

#: Zero-argument factory for the leader gate's async context manager. Injected by
#: tests: SQLite has no advisory locks, so leader/not-leader needs substitution.
LeaderGate = Callable[[], AbstractAsyncContextManager[bool]]


async def _outbox_relay_tick(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """
    One pass of the transactional outbox relay: fetch up to OUTBOX_RELAY_BATCH
    unpublished rows, publish each, then mark the results in a second transaction.

    The caller holds the relay leader lock throughout; locking rows in the fetch cannot
    replace it, because that transaction commits before any publish (ADR 0020). Two
    dead-letter exits stop one unpublishable row consuming an oldest-row slot until
    delivery stops for every tenant with no error rate to notice it by (ADR 0001 item
    3): `SchemaValidationError` on the first attempt, anything else after
    `outbox_max_attempts`. `mark_failed` keeps the row and its payload.
    """
    async with session_factory() as session:
        async with session.begin():
            repo = OutboxRepository(session)
            events = await repo.fetch_unpublished(limit=OUTBOX_RELAY_BATCH)

    # "A pass ran." Inside the tick, after the fetch that proves the queue was
    # reachable, because the loop keeps turning while the relay skips its work.
    # Written whether or not anything was delivered: it is the only thing telling a
    # reader in another process an idle relay from a stopped one (ADR 0028). Never
    # fatal — see `record_relay_tick`.
    await record_relay_tick()

    await _emit_outbox_gauges(session_factory)

    if not events:
        return

    max_attempts = get_settings().outbox_max_attempts
    published_ids: list[uuid.UUID] = []
    retry_ids: list[uuid.UUID] = []
    dead_lettered: list[tuple[uuid.UUID, str]] = []
    for event in events:
        try:
            await kafka_producer.publish_raw(
                topic=event.topic, key=event.key, payload=event.payload
            )
            published_ids.append(event.id)
        except SchemaValidationError as exc:
            dead_lettered.append((event.id, f"schema validation failed: {exc}"))
            logger.error(
                "outbox row dead-lettered — payload does not match its schema",
                extra={
                    "outbox_id": str(event.id),
                    "topic": event.topic,
                    "error": str(exc),
                },
            )
        except Exception as exc:
            # `attempts` is the value read in step 1; this failure is the
            # increment that has not been written yet.
            attempts = (event.attempts or 0) + 1
            if attempts >= max_attempts:
                dead_lettered.append(
                    (event.id, f"abandoned after {attempts} attempts: {exc}")
                )
                logger.error(
                    "outbox row dead-lettered — attempt cap reached",
                    extra={
                        "outbox_id": str(event.id),
                        "topic": event.topic,
                        "attempts": attempts,
                        "error": str(exc),
                    },
                )
            else:
                retry_ids.append(event.id)
                logger.warning(
                    "outbox publish failed, will retry",
                    extra={
                        "outbox_id": str(event.id),
                        "topic": event.topic,
                        "attempts": attempts,
                        "error": str(exc),
                    },
                )

    async with session_factory() as session:
        async with session.begin():
            repo = OutboxRepository(session)
            await repo.mark_published(published_ids)
            await repo.increment_attempts(retry_ids)
            # One statement per row: each carries its own error text, and
            # dead-lettering is rare enough that batching would buy nothing.
            for row_id, error in dead_lettered:
                await repo.mark_failed([row_id], error)

    if dead_lettered:
        await metrics.emit_count("OutboxDeadLettered", float(len(dead_lettered)))

    if published_ids or dead_lettered:
        logger.info(
            "outbox batch published",
            extra={
                "published": len(published_ids),
                "failed": len(retry_ids),
                "dead_lettered": len(dead_lettered),
            },
        )


async def _outbox_relay_loop(
    session_factory: async_sessionmaker[AsyncSession],
    leader_gate: LeaderGate | None = None,
) -> None:
    """Transactional outbox relay — single-writer across replicas (E1-15).

    `worker_loop` runs in every API replica's lifespan, so without the Postgres
    advisory lock a rolling-deploy overlap republishes the whole backlog; the loser
    just sleeps and retries, so leadership follows whoever is up. The `job_events`
    unique constraint and WO-P4-03's atomic claim still dedupe downstream (ADR 0020).
    """
    gate: LeaderGate = leader_gate or (
        lambda: advisory_leader_lock(session_factory, OUTBOX_RELAY_LOCK_KEY)
    )
    while True:
        try:
            async with gate() as is_leader:
                if is_leader:
                    # INSIDE the gate, not in front of it: checking first would
                    # make a paused replica stop contending for leadership, moving
                    # it for a reason that has nothing to do with leadership.
                    if not await loop_is_paused(ControlLoopName.OUTBOX_RELAY):
                        await _outbox_relay_tick(session_factory)
                else:
                    logger.debug("outbox relay tick skipped — not the leader")
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("outbox relay error", extra={"error": str(exc)})

        await asyncio.sleep(OUTBOX_RELAY_INTERVAL)


BACKPRESSURE_LAG_KEY = "kafka:consumer_lag:worker-dispatcher"

# The same number with its measurement time, kept for the last several passes:
# `BACKPRESSURE_LAG_KEY` is one undated integer overwritten every pass, so a climbing
# lag was unverifiable (WO-R3-254). A SECOND key, because `check_backpressure` fixes
# the value key's shape. JSON list, newest first:
# [{"lag": int, "measured_at": ISO-8601 UTC}].
#
# The key, the bound and both TTLs are imported from `app/core/consumer_lag.py`
# (WO-R3-328, and the clock itself since WO-R3-338) rather than mirrored here: the window
# is fifteen minutes of history however fast the pass runs, and the value key's TTL is
# three passes. They were literals in two files and drifting them would have shortened the
# chart without shortening the axis. `test_consumer_lag_history.py` still pins the pair.
LAG_SAMPLES_KEY = _samples_key(LIVE_REFRESHED_GROUP)
LAG_SAMPLES_TTL = _LAG_SAMPLES_TTL


async def _record_lag_sample(redis: Any, lag: int) -> None:
    """Record one measurement for this worker's own group. See
    `app.core.consumer_lag.record_lag_sample` for what it writes and why."""
    await record_lag_sample(redis, lag, group=LIVE_REFRESHED_GROUP)


async def _digest_loop(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Periodic incident-summary digest worker.

    Sleeps `llm_digest_interval_hours` between batches, including when the feature
    flag is off, so the loop never busy-waits.
    """
    from app.services import incident_digest

    while True:
        try:
            settings = get_settings()
            interval_seconds = max(60, settings.llm_digest_interval_hours * 3600)
            await asyncio.sleep(interval_seconds)
            # After the sleep, where the work is. The interval is hours, so a
            # shorter pause expires before a tick ever reads the key
            # (`control_loop_pause.tick_interval_seconds`).
            if await loop_is_paused(ControlLoopName.DIGEST):
                continue
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


# How often a disabled SLO loop re-reads its own setting. Short enough that
# re-enabling evaluation does not need a redeploy, long enough to be free.
_SLO_DISABLED_RECHECK_SECONDS = 60.0

_IDEMPOTENCY_REAPER_INTERVAL_SECONDS = 3600.0  # 1h — matches the TTL cadence


async def _slo_evaluation_loop(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Evaluate the SLOs on a schedule and alert on a fast burn (WO-R2-29).

    Before this, `services/slo.compute_all` had one caller — a read-only admin
    endpoint — so the alert webhook's only producer was a chaos tool. Deliberately NOT
    leader-gated (ADR 0020 is about the relay): the unique constraint on `(tenant_id,
    dedup_key)` makes a second replica lose the insert, and it holds across a handover
    where a gate would not. The interval is read every pass, so 0 disables evaluation.
    """
    from app.services import slo

    while True:
        try:
            interval = get_settings().slo_evaluation_interval_seconds
            if interval <= 0:
                # Disabled: still sleep, and still re-read the setting next pass.
                await asyncio.sleep(_SLO_DISABLED_RECHECK_SECONDS)
                continue
            await asyncio.sleep(interval)
            if await loop_is_paused(ControlLoopName.SLO_EVALUATION):
                continue
            created = await slo.run_evaluation(session_factory)
            if created:
                logger.warning(
                    "SLO evaluation raised fast-burn alerts",
                    extra={"alert_ids": [str(a) for a in created]},
                )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("slo evaluation loop error", extra={"error": str(exc)})


async def _idempotency_reaper_loop(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Delete expired idempotency records every hour.

    Closes ADR 0010's "no reaper means expired records accumulate" consequence.
    Housekeeping, not correctness — lookups already treat expired records as absent
    via `expires_at < now()`, so the hourly cadence only bounds table growth.
    """
    from app.repositories.idempotency import IdempotencyRepository

    while True:
        try:
            await asyncio.sleep(_IDEMPOTENCY_REAPER_INTERVAL_SECONDS)
            if await loop_is_paused(ControlLoopName.IDEMPOTENCY_REAPER):
                continue
            async with session_factory() as session:
                async with session.begin():
                    reaped = await IdempotencyRepository(session).delete_expired()
            if reaped:
                logger.info(
                    "idempotency reaper deleted expired records",
                    extra={"count": reaped},
                )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error(
                "idempotency reaper loop error", extra={"error": str(exc)}
            )


async def _metrics_loop(
    redis: Any,
    consumer: JobDispatcherConsumer,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Emit queue/in-flight/consumer-lag gauges on the configured metrics interval.

    Lag is also cached in Redis for the backpressure check (no per-request Kafka
    query), and each measurement is appended to `LAG_SAMPLES_KEY` so a reader can
    tell a climbing lag from a flat one without waiting a whole pass itself.

    The interval is a setting read every pass (`metrics_interval_seconds`), so the demo
    stack's 5 s is an env var rather than a redeploy of this module (O-35), and the value
    key's TTL is derived from it rather than fixed.

    Since WO-R3-338 the platform's own alert rules run on this same tick, right after the
    measurement they read (ADR 0039). Same clock on purpose: a rule evaluating between two
    measurements can only re-read the number it already saw.
    """
    while True:
        try:
            await asyncio.sleep(metrics_interval_seconds())
            if await loop_is_paused(ControlLoopName.METRICS):
                continue
            delayed = await queue.delayed_length(redis)
            lag = await consumer.consumer_lag()
            await metrics.emit_gauge("QueueDepth", float(delayed))
            await metrics.emit_gauge("InFlightJobs", float(len(consumer.in_flight)))
            # `lag is None` means unknown (not started, no assignment, query
            # errored). Do NOT emit a fabricated 0 — `check_backpressure` and
            # `get_consumer_lag` would read it as healthy. The previous cache
            # entry TTLs out and backpressure fails open on absence.
            if lag is not None:
                await metrics.emit_gauge("ConsumerLag", float(lag))
                await redis.set(
                    BACKPRESSURE_LAG_KEY, lag, ex=lag_value_ttl_seconds()
                )
                # Value first, history second: backpressure's key is the one with
                # a caller waiting on it, and the history is best effort.
                try:
                    await _record_lag_sample(redis, lag)
                except Exception as exc:
                    logger.warning(
                        "consumer lag sample not recorded",
                        extra={"error": str(exc)},
                    )
            # Last, and in its own handler: the rules read what this pass just recorded,
            # and a rule that fails must cost neither the gauges nor the next pass.
            try:
                await alert_rules.evaluate_alert_rules(session_factory, redis)
            except Exception as exc:
                logger.error("alert rule pass failed", extra={"error": str(exc)})
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("metrics loop error", extra={"error": str(exc)})


_SUPERVISOR_POLL_SECONDS = 2.0
_SUPERVISOR_MAX_BACKOFF_SECONDS = 30.0


async def _restart_consumer(consumer: BaseKafkaConsumer) -> None:
    """stop()+start() with capped exponential backoff until it sticks."""
    backoff = 1.0
    while True:
        try:
            await consumer.stop()
            await consumer.start()
            logger.warning(
                "supervisor restarted consumer",
                extra={"group_id": consumer.group_id},
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "consumer restart failed; retrying",
                extra={"group_id": consumer.group_id, "error": str(exc)},
            )
            await asyncio.sleep(min(backoff, _SUPERVISOR_MAX_BACKOFF_SECONDS))
            backoff *= 2


async def _supervise_consumer(consumer: BaseKafkaConsumer) -> None:
    """Own one consumer's whole lifecycle: the first start(), then run() across
    chaos kills and crashes.

    `kill_consumer` makes run() exit and `restart_consumer_group` only deletes the
    Redis kill key, so without this a killed consumer stayed dead until the process
    restarted. The boot start() uses the crash path's backoff helper, guarded BEFORE
    the loop so an orderly stop() cannot resurrect it. run(): raises -> backoff and
    restart; `chaos_killed` -> poll until the kill key is observed absent (a failed
    lookup is not absent), then restart; otherwise -> supervision ends.
    """
    if not consumer.is_running:
        # Boot (or a start() that never took). stop() on a never-started
        # consumer is a safe no-op, so this is exactly the crash path.
        await _restart_consumer(consumer)

    while True:
        try:
            await consumer.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "consumer run() crashed; supervisor restarting",
                extra={"group_id": consumer.group_id, "error": str(exc)},
            )
            await asyncio.sleep(_SUPERVISOR_POLL_SECONDS)
            await _restart_consumer(consumer)
            continue

        if consumer.chaos_killed:
            logger.warning(
                "consumer killed by chaos; supervisor waiting for kill key",
                extra={"group_id": consumer.group_id},
            )
            # Fail CLOSED: only an observed-absent key releases the consumer. A
            # lookup error is "unknown", and unknown must not read as "cleared" —
            # the hook that killed this consumer may be saturating Redis.
            while True:
                try:
                    if not await _check_chaos_kill_strict(consumer.group_id):
                        break
                except Exception as exc:
                    logger.warning(
                        "kill-key lookup failed; holding consumer down",
                        extra={"group_id": consumer.group_id, "error": str(exc)},
                    )
                await asyncio.sleep(_SUPERVISOR_POLL_SECONDS)
            await _restart_consumer(consumer)
            continue

        if consumer.is_running:
            logger.info(
                "consumer run() returned without stop/chaos "
                "(cancelled during shutdown?); supervisor exiting",
                extra={"group_id": consumer.group_id},
            )
        return


async def worker_loop(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Any,
) -> None:
    """Start the 8 Kafka consumers (each its own group) and the 11 background loops.

    Consumers are handed to `_supervise_consumer` UNSTARTED — it owns start() (ADR
    0009 amendment) — so a transient boot error is retried with capped backoff instead
    of dropping that group for the process's life. Each loop reads `chaos:pause:<loop>`
    once per iteration and skips its work while set (`control_loop_pause.py`, ADR
    0027); the consumer groups are NOT in that enum, `kill_consumer` stops those. Under
    `CHAOS_ENABLED` one more task rides along — the pool holder, which is a lab task and not a
    twelfth loop (ADR 0031). Cancel signal: cancel all, wait for in-flight jobs, stop all.
    """
    dispatcher = JobDispatcherConsumer(session_factory, redis)
    audit = AuditConsumer(session_factory)
    sse = SseConsumer(redis)
    event_log = EventLogConsumer(session_factory)
    read_model = ReadModelProjector(redis)
    dep_resolver = DependencyResolver(session_factory, redis)
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

    logger.info(
        "worker loop started", extra={"consumers": [c.group_id for c in consumers]}
    )
    tasks = [asyncio.create_task(_supervise_consumer(c)) for c in consumers]
    tasks.extend(
        [
            asyncio.create_task(_promote_delayed_loop(session_factory, redis)),
            asyncio.create_task(_promote_dlq_replay_loop(session_factory, redis)),
            asyncio.create_task(
                _resume_unblocked_waiting_loop(session_factory, redis)
            ),
            asyncio.create_task(
                _requeue_stale_pending_loop(session_factory, redis)
            ),
            asyncio.create_task(_outbox_relay_loop(session_factory)),
            asyncio.create_task(_metrics_loop(redis, dispatcher, session_factory)),
            asyncio.create_task(_digest_loop(session_factory)),
            asyncio.create_task(_idempotency_reaper_loop(session_factory)),
            asyncio.create_task(
                _stale_running_sweep_loop(session_factory, dispatcher)
            ),
            asyncio.create_task(
                _renew_running_leases_loop(session_factory, dispatcher)
            ),
            asyncio.create_task(_slo_evaluation_loop(session_factory)),
        ]
    )

    if get_settings().chaos_enabled:
        # Not background loops and deliberately not in `ControlLoopName`: they exist only under
        # the chaos gate, and each one's off switch is its own key (ADR 0031, ADR 0034).
        tasks.append(
            asyncio.create_task(db_pool_hold.hold_db_pool(session_factory, redis))
        )
        tasks.append(
            asyncio.create_task(db_slow_query.run_slow_queries(session_factory, redis))
        )

    # After the loops are running, never before them: a breaker that has never failed
    # should be readable from another process rather than absent until its first failure
    # (ADR 0030), but a diagnostic write must not stand between boot and the first tick.
    await record_registered_breakers(redis)

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
        # stop() is idempotent and safe on a consumer that never started.
        for c in consumers:
            await c.stop()
