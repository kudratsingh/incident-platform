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
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import get_settings
from app.core import metrics
from app.core.leader_lock import OUTBOX_RELAY_LOCK_KEY, advisory_leader_lock
from app.core.logging import get_logger, job_id_var, trace_id_var
from app.core.tracing import extract_context, get_tracer
from app.models.enums import TERMINAL_JOB_STATUSES, JobStatus, JobType
from app.models.job import Job
from app.models.job_dependency import JobDependency
from app.repositories.audit import AuditRepository
from app.repositories.job import JobRepository
from app.repositories.job_dependency import JobDependencyRepository
from app.repositories.outbox import OutboxRepository
from app.services import retry_policy
from app.utils.dag_pause import find_blocking_pause
from app.utils.post_commit import run_post_commit
from app.workers import (
    async_tasks,
    cpu_processors,
    dlq_replay_scheduler,
    kafka_producer,
    queue,
    thread_adapters,
)
from app.workers.audit_consumer import AuditConsumer
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

# Resume sweep: slower than the retry loops on purpose. It only exists
# to catch children whose promotion event has already passed (held by a
# DAG pause, or a missed job.completed), so seconds of latency after a
# pause lifts is fine and the DB scan stays cheap.
_RESUME_SWEEP_INTERVAL = 10  # seconds
# Promotable WAITING rows examined per pass. This bounds *promotable* work
# only — the SQL below excludes rows with an unmet parent, so the limit can
# no longer be consumed by rows that are never going to move (R2-09).
_RESUME_SWEEP_LIMIT = 200

# Delay applied when a delayed-retry promotion fails and the job is pushed
# back onto `jobs:delayed`. Short on purpose: the backoff the job was
# waiting out has already elapsed, this is only spacing against a
# transient DB error, not a new backoff.
_PROMOTE_RETRY_DELAY_SECONDS = 5.0

# Stale-PENDING backstop. Deliberately much slower and much older than the
# retry loop: it exists only for the crash windows nothing else covers, and
# every pass it takes is a pass the normal path already failed to take.
_STALE_PENDING_SWEEP_INTERVAL = 60  # seconds between passes
_STALE_PENDING_AGE_SECONDS = 300  # how long PENDING-without-progress is "stale"
_STALE_PENDING_LIMIT = 100  # PENDING rows examined per pass

# E1-08 / ADR 0011 amendment. Delay applied when a dispatch is held back
# because the job's DAG is paused: the work is pushed onto `jobs:delayed`
# to be re-evaluated rather than dropped. 10s matches the resume sweep's
# cadence, so a held retry resumes on the same clock as a held child.
_PAUSE_RECHECK_SECONDS = 10.0

# Same idea for a *scheduled* DLQ replay whose DAG is paused, but on the
# replay's own ZSET and with a longer window: an operator-scheduled replay
# is not latency-sensitive, and re-scheduling keeps
# `_promote_dlq_replay_loop`'s deliberate no-re-enqueue-on-failure policy
# for real failures.
_PAUSED_REPLAY_DEFER_SECONDS = 30

# E1-17 / ADR 0019. Crash-recovery sweep for jobs stranded in RUNNING by a
# hard worker crash. Slow on purpose: the *age* threshold that decides what
# is an orphan is `settings.stale_running_threshold_seconds` (900s), so a
# minute of scan latency on top of it is noise, and the scan stays cheap.
_STALE_RUNNING_SWEEP_INTERVAL = 60.0  # seconds between passes
_STALE_RUNNING_SWEEP_LIMIT = 100  # RUNNING rows examined per pass

# WO-R2-28. The lease that tells one replica's sweep that another replica is
# still executing a job. `jobs.heartbeat_at` is renewed every
# `_RUNNING_LEASE_RENEW_INTERVAL` and read as live for
# `_RUNNING_LEASE_TTL_SECONDS` after the last renewal.
#
# The ratio is what matters: six renewal attempts fit inside one TTL, so a
# transient database blip, a slow pass or a GC pause cannot expire a lease on
# a healthy worker. Widening the TTL further only delays real crash recovery,
# which the age threshold (900s) already dominates; narrowing it towards the
# renewal interval trades a false `job.dlq` for nothing.
_RUNNING_LEASE_RENEW_INTERVAL = 20.0  # seconds between check-ins
_RUNNING_LEASE_TTL_SECONDS = 120.0  # how long a check-in vouches for a job

MAX_CONCURRENT_JOBS = 10  # cap on simultaneously running jobs

# WO-R2-07 / ADR 0021. How long a job may stay RUNNING *past* the
# stale-RUNNING threshold before the sweep reclaims it despite being one of
# this process's in-flight ids.
#
# ADR 0019 made that exclusion unconditional, which was right while execution
# was unbounded — the sweep could not tell a slow job from a stuck one, so it
# had to assume slow. `job_execution_timeout_seconds` now draws that line:
# a local job still RUNNING long past its own deadline is stuck, not slow,
# and the exclusion has to lapse or it is once again the one state nothing
# recovers. The grace only has to cover the deadline breach plus the
# dead-letter write it triggers; reaping inside that window would fan out a
# spurious `job.dlq` and then be overwritten by the write already in flight.
#
# Sized against the threshold, not the deadline, because the sweep's SQL has
# already filtered to rows older than the threshold by the time this applies.
_IN_FLIGHT_EXCLUSION_GRACE_SECONDS = 300.0

# Cap on jobs dispatched but not yet finished — running plus waiting for a
# concurrency slot. `handle_message` no longer blocks on the semaphore (that
# stalled the poll loop and got the group evicted), so without a cap a
# saturated worker would spawn a task per message without limit.
#
# Past the cap `handle_message` raises `DispatchBacklogFull`, which is the
# base consumer's existing backpressure primitive: the offset is not
# committed and the partition seeks back for redelivery. Crucially that
# happens *without* the poll loop stopping — `getmany()` keeps being called
# and the group keeps its member, which is the whole point of the fix.
_MAX_DISPATCH_BACKLOG = MAX_CONCURRENT_JOBS * 10


class JobExecutionTimeout(Exception):
    """A processor overran `job_execution_timeout_seconds`.

    Deliberately NOT a bare `TimeoutError`. Processors raise `TimeoutError`
    themselves all the time (an HTTP client giving up on an upstream), and
    that is an ordinary transient failure that has earned its retries.
    Only the dispatcher's own deadline dead-letters, so the two must be
    distinguishable at the `except` — see `_execute_processor`.
    """


class DispatchBacklogFull(Exception):
    """Raised by `handle_message` when the dispatch backlog is at its cap.

    Signals the base consumer to leave the offset uncommitted and seek back,
    so the message is redelivered once the worker has drained.
    """

# Strategy map: job type → processor coroutine
_PROCESSORS = {
    JobType.BULK_API_SYNC: async_tasks.process_bulk_api_sync,
    JobType.CSV_UPLOAD: thread_adapters.process_csv_upload,
    JobType.DOC_ANALYSIS: cpu_processors.process_doc_analysis,
    JobType.REPORT_GEN: cpu_processors.process_report_gen,
}

# The DLQ event's payload shape moved to `app/schemas/job_events.py` when
# `JobRepository.update_status` became the single producer of terminal events
# (ADR 0001 addendum). Nothing in this module builds a terminal payload by hand
# any more — writing the status IS emitting the event.


def _execution_timeout_seconds(job_type: str) -> float:
    """The execution deadline for `job_type`, in seconds.

    One knob for every type today. The seam exists because the processors do
    not share a cost model — csv_upload runs on a 4-thread pool, doc_analysis
    and report_gen on a process pool — so the first per-type deadline has an
    obvious place to go that is not a call site.
    """
    return float(get_settings().job_execution_timeout_seconds)


async def _execute_processor(
    processor: Any,
    payload: dict[str, Any],
    publish: Any,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run `processor` under a hard deadline.

    Raises `JobExecutionTimeout` when the deadline expires, and lets every
    other exception through untouched — including a `TimeoutError` the
    processor raised itself, which is an ordinary transient failure with
    retries still owed to it. `asyncio.timeout` re-raises an inner
    `TimeoutError` unchanged, so `expired()` is the only thing that reliably
    tells "we cancelled it" from "it gave up": catching bare `TimeoutError`
    around the call would silently dead-letter every upstream blip.

    Cancellation reaches the processor at its next await point. One caveat
    worth stating plainly: a processor parked in `run_in_executor` unblocks
    *here* immediately, but the thread or process it handed the work to runs
    to completion regardless — neither can be preempted in Python. The
    concurrency slot and the job row are released either way, which is what
    the finding is about; the leaked pool worker is a narrower problem and is
    recorded in ADR 0021 rather than papered over here.
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

    # ------------------------------------------------------------------ #
    # 1. Load job and atomically claim PENDING -> RUNNING                  #
    # ------------------------------------------------------------------ #
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
            max_retries = job.max_retries
            prior_error = job.error_message  # filled when this is a retry
            # E1-04: the status check above is only a cheap pre-filter (and
            # a distinct log line) — under at-least-once delivery a second
            # delivery of this job can pass it concurrently. The atomic
            # conditional UPDATE (WHERE status='pending') is the
            # authoritative gate: exactly one delivery wins. It must stay
            # in THIS short transaction — the winner's commit happens at
            # the end of this block, before processor execution, so the
            # loser's UPDATE re-evaluates against the committed row and
            # matches zero rows.
            # E1-08: pre-claim pause re-check. Every other pause probe is
            # at promotion time, which makes them all advisory — a
            # `job.submitted` already sitting in Kafka when `pause_dag`
            # lands would still claim RUNNING and execute. Probed inside
            # this transaction (one parents() query + one Redis MGET) so
            # no extra session is opened; the pause is only *held* after
            # the block, because `push_delayed` must not run inside the
            # DB transaction.
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
        # Status stays PENDING — the job is re-dispatched by the delayed
        # set once the pause lifts, exactly like a held retry. Dropping it
        # here would strand the job until the stale-PENDING backstop.
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
            # Terminal on the first breach — deliberately NOT a retry.
            #
            # The deadline is a function of the payload, and a retry does not
            # change the payload: three more attempts would spend three more
            # full deadlines, holding a concurrency slot each, to arrive back
            # here. (The pathological shape this fix exists for —
            # `{row_count: 1_000_000, chunk_size: 1}` — is ~22h of chunk
            # reads; the deadline turns that into 10 minutes, and retrying it
            # would turn it back into 40.) Dead-lettering routes it straight
            # into the machinery that already exists for jobs needing a human
            # or agent decision: the DLQ tab, LLM triage, saga compensation,
            # Tier-1 replay.
            #
            # `retry_count` is left alone for the same reason the crash sweep
            # leaves it alone (ADR 0019) — it is the attempt history triage
            # and the DLQ tab reason about, and this was not an attempt that
            # failed on its merits.
            span.record_exception(exc)
            span.set_status(trace.StatusCode.ERROR, str(exc))
            error = f"Execution timed out: {exc}"
            async with session_factory() as session:
                async with session.begin():
                    repo = JobRepository(session)
                    audit = AuditRepository(session)
                    # Terminal status and `job.dlq` outbox row in one write,
                    # via the single writer (ADR 0001 addendum). A hand-rolled
                    # status write here would kill the job in Postgres with no
                    # consumer hearing: saga stranded, id pinned in the read
                    # model, triage never run, SSE never closed.
                    await repo.update_status(
                        job_id,
                        JobStatus.DEAD_LETTER,
                        extra={
                            "error_message": error,
                            # Badged so the admin DLQ table can tell a job
                            # that overran from one that threw, without an
                            # audit join per row (F2-16).
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
            # Distinct from the aggregate above on purpose: a rise in
            # deadline breaches is a capacity/payload signal, and it is
            # invisible inside the general dead-letter count.
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
                # Guarded like the other three `push_delayed` call sites. The
                # transaction above has already committed status=PENDING and
                # the `job.failed` "retrying" event; if Redis is down, the
                # correct outcome is a job sitting in PENDING with no timer,
                # which `_requeue_stale_pending_once` re-publishes. Letting
                # the error escape instead sent it to `_run_and_release`'s
                # safety net, which terminally dead-lettered a job that still
                # had retries left — Redis is a performance dependency here,
                # never a correctness one.
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
                            # Persisted on the row, not only in the audit
                            # extra_data below, because the admin DLQ table
                            # badges per row and cannot afford an audit join
                            # per row (F2-16). Left unset otherwise: retries
                            # exhausting is the default mechanism and claims
                            # no attribution.
                            job_extra["dead_lettered_by"] = "llm_retry_policy"
                        # `message` is the one field of the DLQ event a call
                        # site still colours; everything else is derived from
                        # the row inside `update_status`, which writes the
                        # `job.dlq` outbox row in this transaction.
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

        # ------------------------------------------------------------------ #
        # 3. Persist result                                                    #
        # ------------------------------------------------------------------ #
        async with session_factory() as session:
            async with session.begin():
                repo = JobRepository(session)
                audit = AuditRepository(session)
                # `update_status` writes the `job.completed` outbox row in
                # this same transaction — the dependency resolver and the
                # saga coordinator both key off that event.
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

    Concurrency: an asyncio.Semaphore bounds *executing* jobs to
    MAX_CONCURRENT_JOBS. The slot is acquired inside the spawned task, never
    in `handle_message` (WO-R2-07, ADR 0021).

    That distinction is the whole finding. `handle_message` runs on the
    consumer's poll loop, so awaiting the semaphore there stopped the loop
    from calling `getmany()`: MAX_CONCURRENT_JOBS slow jobs took the worker
    out of the group entirely once `fetcher_idle_time` passed
    `max_poll_interval_ms`, with nothing to restart it — and the
    stale-RUNNING sweep skipped exactly those in-flight ids, so nothing
    could recover it either. It was described as backpressure, but Kafka
    backpressure that stops polling is eviction on a timer.

    Backpressure now comes from two bounded mechanisms that both keep the
    loop polling: `_MAX_DISPATCH_BACKLOG` (raise `DispatchBacklogFull`, the
    offset is not committed, the partition seeks back) and
    `job_execution_timeout_seconds` (no job holds a slot indefinitely).

    Offset semantics: the base class commits offsets after `handle_message`
    returns. We spawn `_run_and_release` as a background task and return
    immediately, so the offset advances at dispatch time rather than at job
    completion. The trade-off:
      * Pro: high throughput, the consumer is never blocked by a long job.
      * Con: a worker crash between commit-and-completion leaves the job in DB
        as RUNNING with no message left to redeliver it.
      * Con: a job may now be committed while still queued for a slot rather
        than already executing, so a crash in that window leaves it PENDING
        with no message either. That one has a backstop —
        `_requeue_stale_pending_once` re-publishes PENDING rows with no
        progress — where the RUNNING window needs the sweep below.

    Crash recovery for that window is `_stale_running_sweep_loop`, which
    dead-letters RUNNING rows older than `stale_running_threshold_seconds`
    that are not in `self.in_flight_job_ids` (ADR 0019). It does NOT
    re-publish them: a partially-executed job is unsafe to re-run. The
    in-flight set is what stops the sweep from reaping this process's own
    legitimately-long jobs, so it must be populated before the task is
    spawned and cleared only when the task settles.
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
        # Job ids this process is actively executing. Read by
        # `_sweep_stale_running_once` to exclude live work from crash
        # recovery (E1-17). Deliberately separate from `in_flight` above:
        # that holds Task objects for shutdown draining, this answers
        # "is job X mine right now?" without inspecting task internals.
        self.in_flight_job_ids: set[str] = set()

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

        # Bounded backlog, checked but never *awaited* — this method runs on
        # the poll loop and must return promptly no matter how saturated the
        # worker is. Raising leaves the offset uncommitted and seeks the
        # partition back (see `BaseKafkaConsumer._process_one`), so the
        # message returns after the worker drains, while `getmany()` keeps
        # being called and the group keeps its member.
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

        # Claim the id BEFORE spawning the task, not inside it: between
        # `create_task` and the coroutine's first step there is a scheduling
        # gap in which the sweep could run and see the row as an orphan.
        # `_run_and_release`'s finally block is the only place it is dropped.
        self.in_flight_job_ids.add(job_id_str)
        task = asyncio.create_task(self._run_and_release(job_id_str))
        self.in_flight.add(task)
        task.add_done_callback(self.in_flight.discard)

    async def _run_and_release(self, job_id_str: str) -> None:
        # The concurrency slot is taken HERE, inside the task — the poll loop
        # has already moved on. Waiting for capacity is work the dispatcher
        # does in the background, not something the consumer does instead of
        # polling.
        try:
            await self.semaphore.acquire()
        except BaseException:
            # Cancelled while queued for a slot — `worker_loop`'s shutdown
            # path awaits `in_flight`, so this is reachable on an orderly
            # stop. Drop the claim: an id in `in_flight_job_ids` with no task
            # behind it makes the sweep skip a row nobody is executing.
            self.in_flight_job_ids.discard(job_id_str)
            raise

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
            self.in_flight_job_ids.discard(job_id_str)

    async def _force_dead_letter(self, job_id_str: str, error: str) -> None:
        """Best-effort: mark a job DEAD_LETTER when _run_job escapes with an
        unhandled exception. Used only from the _run_and_release safety net.

        The DEAD_LETTER write and its `job.dlq` outbox row are one
        transactional write inside `update_status` (ADR 0001 addendum). Before
        that, this path wrote the status and an audit row and nothing else, so
        the job died in Postgres and no consumer ever heard: the saga stayed
        RUNNING, the read model kept the id in its old status set, triage never
        ran, and the SSE stream never closed.
        """
        try:
            job_id = uuid.UUID(job_id_str)
        except ValueError:
            return
        async with self.session_factory() as session:
            async with session.begin():
                repo = JobRepository(session)
                job = await repo.get_by_id(job_id)
                # Any terminal state, not just DEAD_LETTER. `_run_job` can
                # settle a job COMPLETED and *then* escape (the metrics emit
                # and contextvar reset run after the commit), and overwriting
                # a completed job with DEAD_LETTER now also mints a `job.dlq`
                # event — turning a silent row-level lie into a broadcast one.
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

        Returns `None` for genuinely-unknown states (consumer not started,
        no assignment yet, Kafka query failed) — never 0. The pre-v0.4.6
        shape returned 0 for these paths, which downstream readers
        (backpressure, `get_consumer_lag` MCP tool) couldn't distinguish
        from "lag is really 0 = healthy". A dead consumer looked identical
        to a healthy one, which was one of the noise sources compounding
        the seven-run debug loop. Practice 9: unknown is not healthy.

        `None` propagates: `_metrics_loop` skips the Redis cache write
        (letting the TTL drop the last-known value) and skips the
        CloudWatch gauge emission (an absent metric is more accurate
        than a fabricated 0). `check_backpressure` treats a missing
        cache entry as fail-open — the same behaviour as pre-fix, so no
        regression on the API side.
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

    Shared by every path that re-publishes an existing job (delayed-retry
    promotion, the stale-PENDING backstop, the resume sweep) so a job
    dispatched by a backstop is byte-identical to one dispatched by the
    normal path.

    The resume sweep built its own copy inline until WO-R2-116 — identical at
    the time, which is the only state a duplicated literal is ever observed
    in and the reason the drift shows up later, in whichever path the next
    field was not added to.
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
    """One pass of delayed-retry promotion: pop the due entries and
    re-publish each through the outbox.

    Failure isolation is the whole point (E1-03). `pop_ready_delayed` is
    destructive — the Lua script ZREMs the batch before returning it — so
    every popped id is now held only by this coroutine. The pre-fix shape
    wrapped the entire loop in one `try`, so the first DB error abandoned
    the rest of the batch: those jobs were already out of `jobs:delayed`
    and sat in PENDING with nothing left to publish them.

    So: each item gets its own try/except (the discipline already applied
    to `_promote_dlq_replay_loop`), and — unlike that loop, which
    deliberately does NOT re-enqueue because the operator can see the
    missed replay in the audit trail — a failed delayed retry is pushed
    back onto `jobs:delayed`. A retry has no such audit trail; losing it
    is silent.

    The re-push happens *outside* the failed session context (the session
    is dead once it raised) and in its own try, because Redis can be the
    thing that's broken. If the re-push fails too the job is genuinely
    stranded in PENDING, and only `_requeue_stale_pending_once` recovers
    it.

    A paused DAG takes the same re-push route for the same reason (E1-08,
    ADR 0011 amendment): the retry is held, never dropped.
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
                    # E1-08: a retry is a new dispatch, so the pause has
                    # to hold it. Probed here — after the row exists, so a
                    # deleted job still takes the drop path above — and
                    # before the outbox add, which is the actual dispatch.
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
            # Held, not dropped: the pop already ZREM'd this id, so the
            # "job not found" `continue` above would lose the retry for
            # good. Re-push (outside the transaction) and let the next
            # pass re-evaluate the pause.
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
    """
    Periodically re-queue delayed retry jobs once their backoff has elapsed.

    The re-publish goes through the outbox (not direct Kafka) so the retry
    survives a worker crash between Redis pop and Kafka publish.

    The outer try only has to cover the pop itself now — per-item failures
    are handled inside `_promote_delayed_once`.
    """
    while True:
        # Liveness for the deep health check: this loop turns twice a second
        # and touches Redis and Postgres, so its silence is the closest thing
        # the worker has to "the loops are wedged" (`workers/supervisor.py`).
        worker_tick()
        try:
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

    The eligibility test lives in SQL rather than in the Python loop, and that
    is the whole fix for R2-09. Previously the query was `status == WAITING
    LIMIT 200` and the `unmet_count` check happened per-row afterwards, so
    permanently-blocked children — the ones whose parent is DEAD_LETTER or
    CANCELLED and therefore never reaching COMPLETED — were still *candidates*.
    They consumed the 200 slots and were then discarded, every pass, forever.
    Nothing cascades them away and nothing purges them, so once a tenant
    accumulated 200 of them the sweep promoted nothing for anybody: the set is
    platform-wide, so one tenant's stuck backlog starved every other tenant's
    held children.

    Raising the limit does not fix this — the blocked set grows without bound,
    so any constant is eventually swallowed. Excluding the rows does fix it:
    with `NOT EXISTS (unmet parent)` in the WHERE clause, the LIMIT can only
    ever truncate work that a later pass can still promote.

    `ORDER BY created_at, id` plus the rotating cursor is the second line of
    defence, for the residual case the predicate cannot see: children that are
    promotable in SQL but held back in Python by a DAG pause (the pause lives
    in Redis). More than `_RESUME_SWEEP_LIMIT` of those under one long pause
    would re-starve an unordered query. The cursor makes each pass resume where
    the last stopped, so every eligible row is reached within a bounded number
    of passes instead of depending on whatever order the planner happened to
    return. `id` is in the sort key because `created_at` alone is not unique —
    bulk-created siblings share a timestamp, and a non-deterministic tiebreak
    would let the cursor skip rows.
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
        # TypeDecorator, and an untyped literal would reach the driver as a
        # raw uuid.UUID that SQLite cannot bind.
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

    A short page (fewer rows than the limit) means the tail was reached, so
    the cursor resets to None and the next pass starts from the oldest row
    again — that is the "rotating" part. A full page hands back the last row
    examined, so the next pass continues past it.
    """
    settings = get_settings()
    examined = 0
    # Read the cursor off the last row *inside* the transaction. After the
    # commit these instances are expired, and touching an attribute then
    # would emit a lazy refresh against a closed session.
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
                # The DAG pause lives in Redis, so it cannot be pushed into
                # the query above; it stays a per-row check. A paused child
                # is skipped but still counts against this pass's page,
                # which is exactly what the cursor exists to survive.
                if (
                    await find_blocking_pause(redis, dep_repo, child.id)
                    is not None
                ):
                    continue

                # E1-04: CAS the promotion — the DependencyResolver
                # (or a concurrent sweep pass) may promote the same
                # child first. The loser must skip the outbox add
                # too, or it still mints a duplicate job.submitted.
                if not await job_repo.promote_waiting_to_pending(child.id):
                    continue
                # WO-R2-116: the shared builder, not a fourth hand-assembled
                # copy of the same seven keys. The shapes were identical when
                # this was written; the helper is what keeps them identical
                # after the next field is added to one of them.
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
    """Promote WAITING jobs whose parents are all done and whose DAG is
    no longer paused.

    The DependencyResolver only reacts to `job.completed`. Once
    `pause_dag` became enforcing, a child held during a pause had no
    second chance: the parent's completion event was already consumed,
    so lifting the pause (or letting its TTL expire) would strand the
    child in WAITING forever. This loop is what makes the pause
    *temporary* rather than terminal, and it doubles as a backstop for
    any child whose promotion event was missed.

    Cross-tenant by design — it's a platform-level scheduler, not a
    request path, so it deliberately doesn't go through the
    tenant-scoped `JobRepository.list_jobs`.

    Deliberately NOT leader-gated, unlike `_outbox_relay_loop` (ADR 0020):
    `promote_waiting_to_pending` below is a CAS, so a second replica
    sweeping the same child loses the compare-and-set and skips the outbox
    add with it. Concurrent sweeps are wasted scans, not duplicate events.

    The cursor is per-replica in-memory state, and deliberately so: it is a
    fairness hint, not a correctness mechanism. Two replicas holding
    different cursors just scan different pages, and a restart losing one
    only means that replica starts from the oldest row again.
    """
    cursor: _ResumeCursor | None = None
    while True:
        try:
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
    """One pass of the stale-PENDING backstop: re-publish PENDING jobs that
    nothing is going to pick up.

    Covers the two crash windows the per-item isolation in
    `_promote_delayed_once` cannot (E1-03). Both leave a job PENDING with
    no Redis timer and no Kafka message — invisible to every other loop:

      1. The worker dies between the destructive Lua pop and the outbox
         commit. The ids are already ZREM'd; nothing re-pushes them.
      2. The worker dies between the retry transaction's commit (which
         writes status=PENDING) and `queue.push_delayed`. The job never
         made it into `jobs:delayed` at all.

    The `jobs:delayed` ZSCORE check is what makes this safe to run.
    A hit means the promotion loop still owns the job — it is legitimately
    waiting out a backoff, and the LLM retry policy can set those to
    minutes. Re-publishing then would run the job early, defeating the
    backoff. Only a job that is old AND has no timer is actually orphaned.

    Duplicate-safe by construction, which is why this depends on the
    atomic claim from WO-P4-03: the sweep can still false-positive when
    Kafka consumer lag exceeds the staleness window (the job.submitted is
    real, just not consumed yet), and the resulting second delivery loses
    `JobRepository.claim_for_running` and executes nothing.

    That claim is also why this stays ungated while `_outbox_relay_loop`
    is leader-gated (ADR 0020). Two replicas sweeping the same stale job
    publish two `job.submitted`; exactly one of them wins the claim and
    runs. A leader gate would suppress the redundant scan but not the
    false-positive above, which no gate can see — the CAS is the stronger
    guarantee and the one that must not be removed.

    Cross-tenant by design — same justification as
    `_resume_unblocked_waiting_loop`: a platform-level scheduler, not a
    request path, so it deliberately doesn't go through the tenant-scoped
    `JobRepository.list_jobs`.
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
                        # window. Without it the row stayed inside its own
                        # predicate — nothing about a re-publish changed the
                        # row — so a dispatcher that was behind got the same
                        # job re-published every 60s for as long as the lag
                        # lasted. `IS NULL` keeps rows that predate the
                        # column, and every row that has never been swept,
                        # eligible on the first pass.
                        or_(
                            Job.requeued_at.is_(None),
                            Job.requeued_at < cutoff,
                        ),
                    )
                    .limit(_STALE_PENDING_LIMIT)
                )
            ).scalars()

            for job in rows:
                # `updated_at` is the right staleness signal (not
                # `created_at`): the retry transaction's
                # update_status(PENDING) touches it, so the age measured
                # here is time-since-last-progress.
                if await redis.zscore(queue.DELAYED_KEY, str(job.id)) is not None:
                    continue
                await outbox_repo.add(
                    tenant_id=job.tenant_id,
                    topic=settings.kafka_topic_job_submitted,
                    key=f"{job.tenant_id}:{job.user_id}",
                    payload=_job_submitted_payload(job),
                )
                # Stamped in the SAME transaction as the outbox insert, which
                # is the whole de-duplication guarantee: either both land or
                # neither does, so the sweep can never publish a job it will
                # not remember publishing (nor mark one it did not publish).
                #
                # `updated_at` is pinned to its own value so the ORM's
                # `onupdate` does not fire. Re-publishing is not progress —
                # if it were recorded as progress the operator-visible age
                # would reset every window and a job stuck for an hour would
                # read as five minutes old.
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

    `JobDispatcherConsumer` commits its Kafka offset at dispatch time, so a
    hard worker crash (SIGKILL, OOM, node loss) leaves up to
    MAX_CONCURRENT_JOBS rows in RUNNING with no message left to redeliver
    them and no timer anywhere pointing at them. Nothing else in the tree
    scans for those rows; before this sweep they stayed RUNNING forever.

    Recovery is DEAD_LETTER, never re-publish. The crashed job may have run
    an arbitrary prefix of its processor's side effects, and re-publishing
    would re-run that prefix; dead-lettering instead routes the job into the
    machinery that already exists for jobs needing human or agent judgement
    (DLQ tab, LLM triage, saga compensation, Tier-1 replay). ADR 0019
    records the revisit trigger.

    Three exclusions, all load-bearing:

      * the **lease** — `jobs.heartbeat_at`, renewed by
        `_renew_running_leases_loop` in whichever replica is executing the
        job (WO-R2-28). A job whose lease was renewed within
        `_RUNNING_LEASE_TTL_SECONDS` is someone's live work and is skipped.
        This is the only one of the three that works across replicas, and
        its absence was the finding: `in_flight_job_ids` lives in one
        process's memory, so replica A read every job replica B was
        executing as an orphan and dead-lettered it — firing a real
        `job.dlq` for a job that was running fine. A leader gate would not
        have fixed that, only chosen which replica did it.
        `heartbeat_at IS NULL` reads as stale, because a crash before the
        first check-in is exactly what this sweep exists to reclaim.
      * `dispatcher.in_flight_job_ids` — this process's own live work,
        excluded for `_IN_FLIGHT_EXCLUSION_GRACE_SECONDS` past the threshold
        rather than forever. Reaping a job out from under its own processor
        fires a spurious `job.dlq` and is then overwritten by that
        processor's own terminal write, so the exclusion has to cover the
        deadline breach and the dead-letter write it triggers. It must not
        cover more than that: an unconditional exclusion (ADR 0019 as
        originally written) made a hung local job the one state nothing in
        the tree could reclaim, which is half of WO-R2-07.

        It is kept alongside the lease rather than replaced by it: it needs
        no database round-trip and it still answers correctly when the
        renewal loop itself is the thing that is wedged. The two agree in
        the ordinary case, and where they disagree the local set is the
        more conservative answer for our own rows.
      * the age cutoff, which is compared **in SQL**. `started_at` is
        TIMESTAMP WITH TIME ZONE but SQLite hands back naive datetimes, so
        aware-vs-naive Python math raises TypeError (the same trap
        documented at `repositories/job.py:91-95`). The cutoff is computed
        once as an aware datetime and pushed into the WHERE clause.

    Each survivor is settled in its OWN session and transaction, mirroring
    `_promote_dlq_replay_loop`'s per-item isolation: one row that fails to
    recover must not roll back the recoveries beside it (the E1-03
    antipattern).

    The recovery write is a compare-and-set against the `started_at` and
    `heartbeat_at` this pass observed during its scan (`guard=` on
    `JobRepository.update_status`). Between the scan and the write — two
    separate transactions, with per-row Redis and Postgres work in between —
    the executing replica can renew its lease, or the job can settle and be
    replayed into a fresh RUNNING attempt. Re-reading the row is not enough
    to see that: the check and the write have to be one statement. On a
    refusal the row is left alone and the next pass re-evaluates it.

    Returns the number of jobs dead-lettered.
    """
    now_scan = datetime.now(UTC)
    cutoff = now_scan - timedelta(seconds=threshold_seconds)
    lease_cutoff = now_scan - timedelta(seconds=_RUNNING_LEASE_TTL_SECONDS)

    # Scan in its own read-only session; the recoveries below each open
    # their own. `started_at IS NOT NULL` is belt-and-braces — the RUNNING
    # transition always sets it, but a hand-seeded or legacy row without it
    # must be skipped rather than crash the loop on a NULL comparison.
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    Job.id,
                    Job.tenant_id,
                    Job.user_id,
                    Job.type,
                    Job.retry_count,
                    Job.max_retries,
                    Job.payload,
                    Job.trace_id,
                    Job.started_at,
                    Job.heartbeat_at,
                )
                .where(
                    Job.status == JobStatus.RUNNING,
                    Job.started_at.is_not(None),
                    Job.started_at < cutoff,
                    # The cross-replica exclusion (WO-R2-28). In SQL for the
                    # same reason the age cutoff is: it decides which rows
                    # are candidates at all, and doing it in Python would
                    # spend the 100-row page on jobs that are plainly alive.
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

        # `row.started_at` came back naive on SQLite and aware on Postgres;
        # normalise before subtracting so the observability fields below
        # can't raise. The recovery decision itself was already made in SQL.
        started_at = row.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        stale_seconds = (now - started_at).total_seconds()

        # The in-flight exclusion is no longer permanent (WO-R2-07, amending
        # ADR 0019 §3). It existed because the sweep could not tell a
        # legitimately-slow local job from a stuck one, so it had to assume
        # slow — which made a hung local job the single state no sweep could
        # ever reclaim. `job_execution_timeout_seconds` now draws that line
        # for us: a job of ours still RUNNING this far past the threshold is
        # one whose own deadline should already have fired, so it is stuck.
        # Inside the grace the exclusion still holds, which is what keeps the
        # sweep off a job whose deadline just fired and whose dead-letter
        # write is still in flight.
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
                    # retry_count is deliberately NOT touched. Replay resets
                    # it on purpose; a crash recovery is not a replay, and
                    # zeroing it here would erase the attempt history triage
                    # and the DLQ tab reason about.
                    # The `job.dlq` outbox row lands in this same transaction,
                    # written by `update_status` from the row it just wrote.
                    # It carries the full `DLQ_EVENT_KEYS` set, not just the
                    # schema's required fields: this event fans out to triage,
                    # the saga coordinator and the event log exactly like a
                    # `_run_job` dead-letter, and a key the producer omits
                    # degrades those consumers silently.
                    #
                    # `guard` makes it a compare-and-set against what the
                    # scan saw. The scan and this write are different
                    # transactions, so between them the executing replica
                    # may have renewed its lease (the row is alive after
                    # all) or the job may have settled and been replayed
                    # into a new RUNNING attempt (`started_at` moved). A
                    # re-read cannot close that gap — only doing the check
                    # and the write in one statement can.
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
                        # Refused: the row moved under us. Someone else owns
                        # its outcome — leave it, and let the next pass
                        # re-evaluate from a fresh scan.
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
                            # Distinguished so replay tooling and triage can
                            # tell a crash orphan (nobody was running it)
                            # from a local job that outlived its deadline
                            # (something was, and stopped responding).
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

    The counterpart to `_sweep_stale_running_once`: that one reads the lease,
    this one writes it. Together they replace "is this job in *my* in-flight
    set?" — a question only the process holding the set can answer — with "has
    *anyone* checked in on this job lately?", which every replica can answer
    from the same row. Without it, a second replica read every job the first
    was executing as a crash orphan and dead-lettered it, `job.dlq` and all.

    Snapshot the id set before awaiting: it is mutated by `handle_message` and
    `_run_and_release` on the same event loop, and iterating it across an await
    would risk "set changed size during iteration". A job that finishes during
    the write is renewed harmlessly — the status predicate in
    `renew_running_leases` drops anything no longer RUNNING.

    The renewal deliberately does not cover a job past
    `threshold_seconds + _IN_FLIGHT_EXCLUSION_GRACE_SECONDS`. A worker that
    hangs still runs this loop, so an unconditional renewal would let it
    defend its own stuck job forever — re-creating, through the lease, the
    single unreclaimable state WO-R2-07 removed. Past that age the check-ins
    stop, the lease lapses, and the job is reclaimable by this replica or any
    other. The bound is the same one the in-flight exclusion uses, so the two
    mechanisms lapse together rather than leaving a window where one defends
    a job the other has given up on.

    Returns the number of leases renewed.
    """
    job_ids: list[uuid.UUID] = []
    for job_id_str in list(dispatcher.in_flight_job_ids):
        try:
            job_ids.append(uuid.UUID(job_id_str))
        except ValueError:
            # A malformed id never reached a real row, so it has no lease to
            # renew. `handle_message` accepts whatever the message carried.
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
    """Keep this worker's RUNNING jobs vouched for — see
    `_renew_running_leases_once` for what the lease is and why it stops.

    Failure posture matches every other loop here: log and keep turning. A
    missed check-in is not immediately harmful, because the TTL spans six
    intervals; a *sustained* failure lets the lease lapse, which degrades to
    exactly the pre-fix behaviour (the sweep falls back on the age threshold
    and the local in-flight set) rather than to anything worse.
    """
    settings = get_settings()
    while True:
        try:
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
    `_sweep_stale_running_once` for what it recovers and why it
    dead-letters rather than re-publishes (ADR 0019).

    Orderly shutdown does not need this loop: `worker_loop`'s
    CancelledError path awaits `dispatcher.in_flight` before returning, so
    a graceful stop settles its own jobs. This is for hard crashes only,
    which is why the threshold is generous rather than responsive.
    """
    settings = get_settings()
    while True:
        try:
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
    """One pass of scheduled-DLQ-replay promotion: claim the due entries
    and fire each one.

    Each claimed entry hits `JobService.replay_job`, which writes the
    canonical `job.replayed` audit row and republishes via the outbox —
    so the eventual execution is indistinguishable from an immediate
    replay, aside from the paired `job.replay_scheduled` row written at
    scheduling time.

    A due entry whose DAG is paused is re-scheduled (E1-08) instead of
    fired, keeping the remediation alive until the pause lifts.

    Claim/ack, not pop (R2-21). `claim_ready` moves due members into
    `jobs:dlq_replay_inflight` rather than deleting them, and every
    outcome this pass can *observe* — fired, deferred, or failed — acks
    the claim. Failure still does not re-enqueue: the operator sees the
    `job.replay_scheduled` row with no matching `job.replayed` row and
    re-issues, and auto-retrying would mask a permanent problem like
    "job was deleted". What the ack does buy is the case this pass
    cannot observe: a worker killed mid-replay never reaches the ack, so
    the claim lapses and a later tick recovers the entry instead of
    losing it. `CancelledError` (the shutdown signal) is a
    BaseException, so it bypasses the `except Exception` and its ack by
    construction.
    """
    # Local imports keep the module-level graph flat and match the
    # style of other supporting loops in this file.
    from app.repositories.job_dependency import JobDependencyRepository
    from app.services.job import JobService

    ready = await dlq_replay_scheduler.claim_ready(redis)
    for tenant_id, principal_id, job_id in ready:
        try:
            held_by: uuid.UUID | None = None
            async with session_factory() as session:
                async with session.begin():
                    # E1-08: probe the pause BEFORE the replay
                    # rather than letting `replay_job`'s JobError
                    # be the mechanism. This loop deliberately does
                    # not re-enqueue a failure, so a refusal here
                    # would silently discard the `wait_and_replay`
                    # remediation the operator (or the agent)
                    # scheduled.
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
                        # Scheduled DLQ replays only come from SA
                        # callers today (Tier-1 tools). If we grow a
                        # human path we'd carry principal_type in
                        # the ZSET member; for now, assume SA.
                        await service.replay_job(
                            job_id=job_id,
                            tenant_id=tenant_id,
                            principal_type="service_account",
                            principal_id=principal_id,
                        )
                # This loop owns its own transaction boundary, so it
                # drains the post-commit queue itself — the `get_db`
                # dependency does it for the API and MCP processes but
                # never runs here (R2-23). Inside the session block and
                # before the ack: the commit has landed, so the cache
                # invalidation is safe to publish, and doing it here
                # keeps it on the same success path the ack acknowledges.
                # `run_post_commit` cannot raise, so it cannot turn a
                # committed replay into a logged "fire failed".
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

        # Reached on success, on a paused-DAG deferral (the entry is
        # armed again on the scheduled set, so holding the claim too
        # would replay it twice) and on a logged failure. Skipped only
        # when the worker is going down mid-item — the reclaim case.
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
    """
    Fire operator-scheduled DLQ replays whose delay window has elapsed.

    Distinct from `_promote_delayed_loop` (which handles the retry-cycle
    ZSET `jobs:delayed`). This one drains `jobs:dlq_replay_delayed` —
    entries the agent's Tier-1 tools scheduled via
    `replay_dlq_by_ids(delay_seconds=…)` or
    `replay_dlq_by_category(delay_seconds=…)`. See
    `_promote_dlq_replay_once` for the per-pass semantics.
    """
    while True:
        try:
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

#: How often the relay reports queue health. The tick runs once a second;
#: CloudWatch does not need that, and only the leader emits so the gauge
#: stays one series rather than one per replica.
_OUTBOX_GAUGE_INTERVAL = 60.0
_last_outbox_gauge_at: float = 0.0


async def _emit_outbox_gauges(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Publish outbox depth + oldest-row age, at most once a minute.

    `QueueDepth` cannot cover this. It measures the Redis delayed set, which
    is untouched by a relay stall — the outbox can stop delivering entirely
    while every existing dashboard reads green. These two gauges are the
    only signal that would notice, so their emission failing must not take
    the tick down with it.
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

#: Zero-argument factory returning the leader gate's async context
#: manager. Injected by the tests — the SQLite tiers have no advisory
#: locks, so the real gate is a no-op there and the interesting states
#: (leader / not leader) can only be exercised by substitution.
LeaderGate = Callable[[], AbstractAsyncContextManager[bool]]


async def _outbox_relay_tick(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """
    One pass of the transactional outbox relay:

      1. Read up to OUTBOX_RELAY_BATCH unpublished rows in one transaction.
      2. For each row, attempt to publish to Kafka.
      3. Mark successfully-published rows in a second transaction.
      4. Rows whose publish failed transiently stay unpublished and are
         retried next tick; rows that can never publish are dead-lettered.

    The caller holds the relay leader lock for the whole of this. That is
    load-bearing and cannot be replaced by locking the rows in step 1:
    step 1's transaction commits before a single publish happens, so any
    `FOR UPDATE` taken there is already released by step 2 (ADR 0020).

    Step 4 is why the row-level try/except is not enough on its own. It
    gives per-row *isolation* — one bad payload cannot abort the batch —
    but isolation without an exit means the bad row is fetched again next
    tick, forever. The fetch window is a fixed OUTBOX_RELAY_BATCH oldest
    rows, so a permanently unpublishable row does not degrade throughput
    gradually: it consumes one of exactly 100 slots until 100 of them are
    consumed, and then delivery stops completely, for every tenant, with no
    error rate to notice it by. Two exits, per ADR 0001 Decision item 3:

      * `SchemaValidationError` — deterministic. The same payload will fail
        the same way on every future tick, so retrying is pure cost.
        Dead-letter on the first attempt.
      * anything else `outbox_max_attempts` times over — the backstop for
        failures we cannot classify (a record the broker refuses as
        oversize, a topic that does not exist). Generous by default,
        because a broker outage fails every row in the batch and this must
        not turn a blip into a quarantined backlog.

    Dead-lettering keeps the row and its payload; see `mark_failed`.
    """
    async with session_factory() as session:
        async with session.begin():
            repo = OutboxRepository(session)
            events = await repo.fetch_unpublished(limit=OUTBOX_RELAY_BATCH)

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
    """
    Transactional outbox relay — single-writer across replicas (E1-15).

    `worker_loop` runs in every API replica's lifespan, so without a gate
    every rolling-deploy overlap publishes the entire unpublished backlog
    twice: duplicate lifecycle events into audit, `job_events` and the SSE
    bridge. Each tick therefore runs only if this process wins a Postgres
    advisory lock; the loser sleeps the same interval and tries again, so
    leadership follows whoever is up rather than being pinned to a task.

    Second line of defense, not replaced by this: the `job_events` unique
    constraint and WO-P4-03's atomic claim still dedupe on the consumer
    side. See ADR 0020.
    """
    gate: LeaderGate = leader_gate or (
        lambda: advisory_leader_lock(session_factory, OUTBOX_RELAY_LOCK_KEY)
    )
    while True:
        try:
            async with gate() as is_leader:
                if is_leader:
                    await _outbox_relay_tick(session_factory)
                else:
                    logger.debug("outbox relay tick skipped — not the leader")
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


# How often a disabled SLO loop re-reads its own setting. Short enough that
# re-enabling evaluation does not need a redeploy, long enough to be free.
_SLO_DISABLED_RECHECK_SECONDS = 60.0

_IDEMPOTENCY_REAPER_INTERVAL_SECONDS = 3600.0  # 1h — matches the TTL cadence


async def _slo_evaluation_loop(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Evaluate the SLOs on a schedule and alert on a fast burn (WO-R2-29).

    `services/slo.compute_all` had exactly one caller before this — a
    read-only admin endpoint — so the objectives were only ever computed when
    a human asked, and no real platform condition created an Alert. The alert
    webhook is the incident commander's production trigger, and its only
    producer was a chaos tool: the commander could be woken by a human
    pretending there was an incident, and by nothing else.

    Deliberately NOT leader-gated, for the same reason as the dispatcher
    sweeps (ADR 0020 applies to the outbox relay, not to everything): the
    de-duplication is a unique constraint on `(tenant_id, dedup_key)`, so a
    second replica evaluating the same window loses the insert and stops.
    Concurrent evaluation costs redundant aggregate queries, not duplicate
    alerts — and unlike a gate, that guarantee also holds across a
    leader handover.

    The interval is read every pass rather than captured once, matching
    `_digest_loop`: 0 disables evaluation without a redeploy, for
    deployments that alert from CloudWatch alone and want no second producer.
    """
    from app.services import slo

    while True:
        try:
            interval = get_settings().slo_evaluation_interval_seconds
            if interval <= 0:
                # Disabled. Still sleep, so the loop doesn't busy-wait, and
                # still re-read the setting on the next pass.
                await asyncio.sleep(_SLO_DISABLED_RECHECK_SECONDS)
                continue
            await asyncio.sleep(interval)
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

    Closes the "no reaper means expired records accumulate" negative
    consequence in [ADR 0010](docs/ADR/0010-idempotency-record-lifecycle.md).
    Lookups already treat expired records as absent so this is a
    housekeeping loop, not a correctness one — but at ~10²–10³
    records/tenant/day the table would grow without bound and every
    lookup would scan more rows than necessary.

    The interval mirrors the record TTL cadence (24h). Running hourly
    means a record expires at t+24h, gets reaped no later than t+25h.
    That's a bounded 1h window of "expired but still in the table",
    which lookups handle via the `expires_at < now()` check.
    """
    from app.repositories.idempotency import IdempotencyRepository

    while True:
        try:
            await asyncio.sleep(_IDEMPOTENCY_REAPER_INTERVAL_SECONDS)
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
            # `lag is None` means the consumer is in an unknown state
            # (not started, no assignment, or Kafka query errored). Do
            # NOT emit a fabricated 0 — the reader (`check_backpressure`
            # and `get_consumer_lag`) would treat that as "healthy" and
            # mask a real problem. The previous cache entry TTLs out and
            # backpressure fails open on absence. See ADR-referenced
            # discussion of "unknown is not healthy" (Practice 9).
            if lag is not None:
                await metrics.emit_gauge("ConsumerLag", float(lag))
                await redis.set(BACKPRESSURE_LAG_KEY, lag, ex=BACKPRESSURE_LAG_TTL)
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
    """Own one consumer's whole lifecycle: the first start(), then run()
    across chaos kills and crashes.

    Fills the gap three docstrings referenced but nothing implemented:
    `kill_consumer` makes run() exit and `restart_consumer_group` only
    deletes the Redis kill key — without a supervisor, a killed consumer
    stayed dead until the worker process restarted, so live remediation
    evals could never observe recovery.

    Boot: the consumer arrives unstarted. Starting it here (through the same
    backoff helper the crash path uses) means a transient Kafka/DNS error at
    boot is retried instead of silently dropping that consumer group for the
    life of the process. The guard sits BEFORE the loop on purpose — inside
    it, an orderly stop() (which leaves is_running False) would resurrect the
    consumer during shutdown.

    run() outcomes:
      * raises                 -> log, backoff, stop/start, re-enter.
      * returns, chaos_killed  -> poll until the kill key is observed absent
        (restart_consumer_group or TTL expiry), then stop/start for a
        fresh AIOKafkaConsumer and re-enter run(). A failed lookup is NOT
        an absent key: the consumer stays down until Redis answers.
      * returns otherwise      -> stop() was called (orderly shutdown) or
        run() swallowed a cancellation during teardown; supervision ends.
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
            # Fail CLOSED: only an observed-absent key releases the consumer.
            # A lookup error means "unknown", and unknown must not read as
            # "cleared" — the chaos hook that killed this consumer can be the
            # same one saturating Redis. One warning per 2s poll is the cost.
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
    """
    Start the Kafka consumers and the supporting background loops.

    Every consumer is handed to `_supervise_consumer` UNSTARTED — the
    supervisor owns start() (ADR 0009 amendment). worker_loop therefore never
    drops a consumer group because its first start() hit a transient
    Kafka/DNS error; the supervisor retries with capped backoff until it
    sticks. The corollary is that a permanently unreachable broker leaves all
    8 supervisors retrying rather than disabling the worker.

    Concurrent tasks that make up the worker — 8 Kafka consumers + 11 loops:

      Kafka consumers (each its own group, so failure of one doesn't
      affect the others):
        1. dispatcher.run()       — consumes `job.submitted`, spawns _run_job.
        2. audit.run()            — consumes lifecycle events, writes audit rows.
        3. sse.run()              — consumes lifecycle events, bridges to Redis pub/sub.
        4. event_log.run()        — appends every lifecycle event to `job_events`.
        5. read_model.run()       — projects per-tenant/per-user status sets in Redis.
        6. dep_resolver.run()     — promotes WAITING children to PENDING on parent completion,
                                    unless the child or an ancestor carries a `dag:paused:*` flag.
        7. saga.run()             — settles sagas; enqueues compensation on DLQ.
        8. triage.run()           — Phase 10: LLM classification of dead-lettered jobs.

      Background loops:
        1. _promote_delayed_loop      — re-publishes delayed retries via outbox.
        2. _promote_dlq_replay_loop   — fires operator-scheduled DLQ replays.
        3. _resume_unblocked_waiting_loop — promotes WAITING children once their DAG
                                        pause lifts; backstops missed promotions.
        4. _requeue_stale_pending_loop — backstops the delayed-retry pipeline:
                                        re-publishes PENDING jobs with no
                                        `jobs:delayed` timer left (crash windows).
        5. _outbox_relay_loop         — publishes outbox rows to Kafka.
        6. _metrics_loop              — emits gauges + cached lag for backpressure.
        7. _digest_loop               — Phase 10: per-tenant LLM digests (opt-in).
        8. _idempotency_reaper_loop   — hourly DELETE of expired idempotency records
                                        (ADR 0010's "no reaper" follow-up).
        9. _stale_running_sweep_loop  — dead-letters RUNNING jobs orphaned by a
                                        hard worker crash (ADR 0019).
       10. _renew_running_leases_loop — checks in on this worker's RUNNING jobs
                                        so another replica's sweep can tell
                                        them apart from crash orphans
                                        (WO-R2-28, ADR 0023).
       11. _slo_evaluation_loop      — computes the SLOs on an interval and
                                        raises a de-duplicated Alert on a
                                        fast burn — the alert webhook's only
                                        non-chaos producer (WO-R2-29).

    Cancel signal: cancel all, wait for in-flight jobs, stop all consumers.
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
            asyncio.create_task(_metrics_loop(redis, dispatcher)),
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
