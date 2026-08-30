"""Canonical Kafka payload shapes for the terminal job lifecycle events.

These builders live next to `schemas/kafka/*.schema.json` because they are the
producer half of the contract those files validate. Since the terminal-event
consolidation (see the addendum on [ADR 0001](../../../docs/ADR/0001-outbox-vs-cdc.md))
there is exactly one caller in app code — `JobRepository.update_status` — so a
job cannot reach `dead_letter` or `completed` in Postgres without the matching
outbox row being written in the same transaction.

Everything here is derived from the `jobs` row *after* the status write, which
is what makes a single producer possible: the row already carries the error,
retry counts, payload and trace id that every dead-letter site used to assemble
by hand from local variables.
"""

import json
from typing import Any

from app.models.job import Job

# Ceiling on the serialized job payload copied onto a `job.dlq` event.
# Job payloads are numerically bounded at the creation surfaces but can still
# carry arbitrary user keys up to the request size limit, and this event fans
# out to four consumer groups *and* is appended verbatim to `job_events` by
# the event-log consumer. Anything larger is replaced by a marker so triage
# still learns the payload existed without bloating every downstream row.
DLQ_PAYLOAD_MAX_BYTES = 4096

# The exact key set every `job.dlq` outbox payload carries. It is the producer
# half of a contract whose consumer half is `LlmTriageConsumer.handle_message`
# (plus the saga coordinator and the event log);
# `tests/unit/test_triage_consumer.py` asserts this stays a superset of what
# triage reads, because a key triage reads and the producer never writes
# degrades silently (max_retries → 0, payload/trace_id → None) instead of
# failing. `dlq_event_payload` below is now the only thing that has to keep
# step with it.
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

# Fallback `error` string for a dead-letter whose row carries no
# `error_message`. The `job.failed` schema requires `error` to be a string, so
# a NULL column must not become `None` on the wire — a schema violation would
# mark the outbox row failed and lose the event, which is the exact failure
# this whole consolidation exists to prevent.
_UNSPECIFIED_ERROR = "job dead-lettered without a recorded error"

# The same trap on the cancelled side: `reason` is required and typed string,
# and both writers happen to set `error_message` today (the saga coordinator
# writes "saga rollback", the dependency cascade writes which parent stranded
# the DAG). "Happen to" is why the fallback exists — a third writer that
# forgets would otherwise lose its event to a validation failure rather than
# publish a vague one.
_UNSPECIFIED_CANCEL_REASON = "job cancelled without a recorded reason"

# The OTel carrier is injected into the payload at job creation and popped
# before execution. It must never ride along on a lifecycle event: it is
# tracing plumbing, and it would be appended verbatim to `job_events`.
_TRACE_CARRIER_KEY = "__traceparent"


def payload_for_event(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Bounded copy of a job payload for embedding in a `job.dlq` event.

    Returns the payload unchanged when it serializes to at most
    `DLQ_PAYLOAD_MAX_BYTES`, a `{"_truncated": True, "_original_bytes": n}`
    marker when it doesn't, and `None` when it can't be serialized at all
    (a payload that would break the outbox row must not take the DLQ event
    down with it — triage degrades to "no payload", which is the pre-fix
    behaviour and strictly better than losing the event).
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
    """The `job.dlq` event for a job row that has just been written DEAD_LETTER.

    `message` is the one field a call site can still colour: the exhaustion
    branch says "exhausted after N attempts", the LLM policy says why it gave
    up early. Everything else is read off the row so the four dead-letter sites
    cannot drift from each other again. Defaults to the error itself.
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
        # Triage context (E1-14).
        "max_retries": job.max_retries,
        "payload": payload_for_event(
            {
                k: v
                for k, v in (job.payload or {}).items()
                if k != _TRACE_CARRIER_KEY
            }
        ),
        # The raw column, never `trace_id_var`: that contextvar falls back to
        # the job id when the column is NULL, and a job id masquerading as a
        # trace id sends triage (and anyone following the link) to a trace
        # that doesn't exist.
        "trace_id": job.trace_id,
        "dead_lettered": True,
    }


def cancelled_event_payload(job: Job) -> dict[str, Any]:
    """The `job.cancelled` event for a job row just written CANCELLED.

    CANCELLED was the platform's only terminal status with nothing to announce
    on (WO-R2-113), which made it the only way for a job to stop without any
    consumer finding out: the read model kept the id in whichever status set it
    last saw, the SSE stream stayed open, and the timeline simply ended.

    `reason` rather than `error` because a cancellation is not a failure — the
    job was stood down, by a saga rolling back or by a dependency parent that
    can no longer complete — and the distinction is the whole reason this is
    its own topic instead of a `job.failed` with a flag. Consumers that treat
    the two alike (the event log, the audit writer) are free to; the read model
    and the SLO denominator are not, and merging them would have made that
    impossible to express.
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
