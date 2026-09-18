"""Canonical Kafka payload shapes for the terminal job lifecycle events.

Producer half of the contract `schemas/kafka/*.schema.json` validates. Since the
terminal-event consolidation (ADR 0001 addendum) the only caller is
`JobRepository.update_status`, so a job cannot reach `dead_letter`/`completed` without
its outbox row in the same transaction; every field is read off the `jobs` row.
"""

import json
from typing import Any

from app.models.job import Job

# Ceiling on the serialized job payload copied onto a `job.dlq` event: it fans out to
# four consumer groups and is appended verbatim to `job_events`. Larger → a marker.
DLQ_PAYLOAD_MAX_BYTES = 4096

# The exact key set every `job.dlq` payload carries. `tests/unit/test_triage_consumer.py`
# pins it as a superset of what triage reads, because a key triage reads and the producer
# never writes degrades silently instead of failing. `max_attempts` and `max_retries` are
# the SAME value under two names for one release (WO-R2-172); read `max_attempts` first.
DLQ_EVENT_KEYS: frozenset[str] = frozenset(
    {
        "event",
        "tenant_id",
        "job_id",
        "user_id",
        "job_type",
        "error",
        "message",
        "retry_count",
        "max_attempts",
        # Deprecated alias of `max_attempts`; removed after one release.
        "max_retries",
        "payload",
        "trace_id",
        "dead_lettered",
    }
)

# Same contract for the completed side, read by the dependency resolver (which
# promotes WAITING children) and the saga coordinator (which settles the saga).
COMPLETED_EVENT_KEYS: frozenset[str] = frozenset(
    {
        "event",
        "tenant_id",
        "job_id",
        "user_id",
        "job_type",
        "result",
        "retry_count",
    }
)

# Same contract for the cancelled side (WO-R2-113), read by the read model
# (which moves the id into its `cancelled` set), the SSE bridge (which closes
# the stream), the event log and the audit writer.
CANCELLED_EVENT_KEYS: frozenset[str] = frozenset(
    {
        "event",
        "tenant_id",
        "job_id",
        "user_id",
        "job_type",
        "reason",
        "retry_count",
        "trace_id",
    }
)

# Fallback `error` for a dead-letter with no `error_message` — the `job.failed`
# schema requires a string, and a NULL on the wire would lose the event.
_UNSPECIFIED_ERROR = "job dead-lettered without a recorded error"

# Same trap on the cancelled side: `reason` is required and typed string, and both
# writers only happen to set `error_message` — a third that forgets would lose its event.
_UNSPECIFIED_CANCEL_REASON = "job cancelled without a recorded reason"

# The OTel carrier is injected into the payload at job creation and popped
# before execution. It must never ride along on a lifecycle event: it is
# tracing plumbing, and it would be appended verbatim to `job_events`.
_TRACE_CARRIER_KEY = "__traceparent"


def payload_for_event(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Bounded copy of a job payload for embedding in a `job.dlq` event.

    Unchanged under `DLQ_PAYLOAD_MAX_BYTES`, a `{"_truncated", "_original_bytes"}` marker
    when over, `None` when unserializable — triage degrades, the event survives.
    """
    if payload is None:
        return None
    try:
        size = len(json.dumps(payload).encode("utf-8"))
    except (TypeError, ValueError):
        return None
    if size <= DLQ_PAYLOAD_MAX_BYTES:
        return payload
    return {"_truncated": True, "_original_bytes": size}


def dlq_event_payload(job: Job, message: str | None = None) -> dict[str, Any]:
    """The `job.dlq` event for a job row just written DEAD_LETTER.

    `message` is the only field a call site colours; the rest is read off the row.
    """
    error = job.error_message or _UNSPECIFIED_ERROR
    return {
        "event": "job.failed",
        "tenant_id": str(job.tenant_id),
        "job_id": str(job.id),
        "user_id": str(job.user_id),
        "job_type": job.type,
        "error": error,
        "message": message if message is not None else error,
        "retry_count": job.retry_count,
        # Triage context (E1-14). `max_retries` is the same integer under the
        # name this topic shipped with, kept for one release (WO-R2-172).
        "max_attempts": job.max_attempts,
        "max_retries": job.max_attempts,
        "payload": payload_for_event(
            {
                k: v
                for k, v in (job.payload or {}).items()
                if k != _TRACE_CARRIER_KEY
            }
        ),
        # The raw column, never `trace_id_var`: that falls back to the job id,
        # and a job id masquerading as a trace id points at nothing.
        "trace_id": job.trace_id,
        "dead_lettered": True,
    }


def cancelled_event_payload(job: Job) -> dict[str, Any]:
    """The `job.cancelled` event for a job row just written CANCELLED.

    CANCELLED was the only terminal status with nothing to announce on (WO-R2-113), so a
    job could stop with no consumer finding out. `reason`, not `error`: a cancellation is
    not a failure, and the read model and the SLO denominator must tell them apart.
    """
    return {
        "event": "job.cancelled",
        "tenant_id": str(job.tenant_id),
        "job_id": str(job.id),
        "user_id": str(job.user_id),
        "job_type": job.type,
        "reason": job.error_message or _UNSPECIFIED_CANCEL_REASON,
        "retry_count": job.retry_count,
        "trace_id": job.trace_id,
    }


def completed_event_payload(job: Job) -> dict[str, Any]:
    """The `job.completed` event for a job row just written COMPLETED."""
    return {
        "event": "job.completed",
        "tenant_id": str(job.tenant_id),
        "job_id": str(job.id),
        "user_id": str(job.user_id),
        "job_type": job.type,
        "result": job.result,
        "retry_count": job.retry_count,
    }
