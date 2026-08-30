"""
Server-Sent Events endpoint for live job progress.

POST /api/v1/jobs/{job_id}/stream-token  — mint a short-lived stream token
GET  /api/v1/jobs/{job_id}/stream?token= — the SSE stream itself

The client opens the stream once and receives text/event-stream events as the
worker publishes progress.  The connection closes automatically when the job
reaches a terminal state (completed / failed / dead_letter / cancelled) —
including for a client that connects *after* the job finished: the stream
opens with the retained progress snapshot (`job:progress:last:{job_id}`), and
when no snapshot survives, a finished `jobs` row is turned into one synthetic
terminal event.  A late subscriber is never left on a silent open connection.

Auth transport (ADR 0014): native EventSource cannot set request headers, so
the GET cannot carry the usual Authorization header.  The client first POSTs
for a stream token (a normal fetch, normal header auth) — that endpoint
authorizes the job (tenant scope + ownership) and mints a single-purpose
token bound to this job_id.  The GET then validates that token from the query
string; its identity comes entirely from the token.  A leaked stream URL is
low-value: the token expires in STREAM_TOKEN_TTL_SECONDS and opens nothing
but this one job's stream.

Why SSE over WebSockets here:
  - Job progress is unidirectional (server → client only).
  - SSE reconnects automatically in the browser.
  - No need for a full duplex channel.

Connection budget (WO-R2-11): an open stream no longer owns a Redis
connection.  Every stream in the process reads off one shared Pub/Sub
connection (`workers/progress_broker.py`) drawn from a pool dedicated to
streaming (`core/redis.py`), so viewers can no longer starve the rate limiter,
`check_backpressure` and the worker loops that share the default pool.  The
number of concurrent streams is capped explicitly instead — past the cap this
endpoint answers 503 + `Retry-After` — and idle/maximum-duration timeouts stop
a parked tab from holding a slot forever.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from app.core.exceptions import AuthenticationError, AuthorizationError
from app.core.redis import get_redis
from app.core.security import create_stream_token, decode_token
from app.core.tenant_scope import declare_tenant_scope
from app.dependencies import get_current_user, get_db
from app.models.enums import JobStatus
from app.models.job import Job
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.job import JobRepository
from app.repositories.outbox import OutboxRepository
from app.schemas.job import StreamTokenResponse
from app.services.job import JobService
from app.workers.progress import (
    TERMINAL_STATUSES,
    ProgressEvent,
    read_last_event,
)
from app.workers.progress_broker import acquire_stream_slot, subscribe
from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse
from starlette.background import BackgroundTask

router = APIRouter(tags=["streaming"])


@router.post("/jobs/{job_id}/stream-token", response_model=StreamTokenResponse)
async def issue_stream_token(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> StreamTokenResponse:
    """
    Mint a short-lived, single-purpose token for this job's SSE stream.

    This is where the stream's authorization happens: get_job raises 404 for
    a cross-tenant job (existence is never confirmed) and 403 for a non-owner
    without the admin/support role.  Only then is the token minted, with the
    job id as its subject so it cannot be replayed against another job.
    """
    svc = JobService(
        JobRepository(db),
        AuditRepository(db),
        OutboxRepository(db),
        redis,
    )
    await svc.get_job(
        job_id=job_id,
        requesting_user_id=current_user.id,
        user_role=current_user.role,
        tenant_id=current_user.tenant_id,
    )
    return StreamTokenResponse(token=create_stream_token(job_id, current_user.tenant_id))


def _terminal_event_from_row(job: Job) -> ProgressEvent:
    """The one event a caller gets when the job finished before they connected.

    Built from the dataclass (never hand-rolled JSON) so it is indistinguishable
    from a published event on the wire — the frontend types mirror this shape.
    """
    return ProgressEvent(
        job_id=str(job.id),
        status=job.status,
        progress=100 if job.status == JobStatus.COMPLETED else 0,
        message=job.error_message or "Job already finished",
        retry_count=job.retry_count,
    )


def _snapshot_is_stale(
    snapshot: ProgressEvent | None,
    *,
    row_is_terminal: bool,
    row_updated_at: datetime | None,
) -> bool:
    """True when the retained snapshot says 'finished' and the row disagrees.

    A DLQ replay is the way this happens: the job reaches `dead_letter`, the
    snapshot records it, and then an operator replays the job. The replay's
    `running` events deliberately do not overwrite a terminal snapshot
    (`workers/progress.py`), so for the snapshot's remaining TTL it describes
    a lifecycle the job has already left — and because `subscribe()` ends on
    the first terminal event, a viewer of a job that is running right now got
    one `dead_letter` event and a closed stream.

    The tie-break is recency, not a preference for the row: the row is only
    believed if it was written *after* the snapshot. That matters for the
    ordinary race where a job finishes microseconds after this request read
    its row — there the snapshot is the newer of the two, it is honoured, and
    the client is correctly told the job is done instead of waiting out the
    broker's idle timeout for events that have already been published.

    Anything unparseable leaves the snapshot in charge, which is the
    pre-WO-R2-57 behaviour.
    """
    if snapshot is None or row_is_terminal or row_updated_at is None:
        return False
    if snapshot.status not in TERMINAL_STATUSES:
        return False
    try:
        snapshot_at = datetime.fromisoformat(snapshot.timestamp)
    except (TypeError, ValueError):
        return False
    if snapshot_at.tzinfo is None:
        snapshot_at = snapshot_at.replace(tzinfo=UTC)
    row_at = (
        row_updated_at.replace(tzinfo=UTC)
        if row_updated_at.tzinfo is None
        else row_updated_at
    )
    return row_at > snapshot_at


@router.get("/jobs/{job_id}/stream")
async def stream_job_progress(
    job_id: uuid.UUID,
    token: str | None = Query(default=None),
    redis: Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
) -> EventSourceResponse:
    """
    Stream live progress events for a job via Server-Sent Events.

    Events are JSON-encoded ProgressEvent objects:
      { job_id, status, progress, message, retry_count, timestamp }

    The stream closes when status is one of:
    completed | failed | dead_letter | cancelled.

    That promise now holds for late subscribers too.  Redis Pub/Sub is
    at-most-once, so a client that connects after the terminal event — or that
    reconnects across a Redis blip — used to sit on a silent open connection
    forever.  Two things close it now: `subscribe` opens with the retained
    `job:progress:last:{job_id}` snapshot and ends immediately if that snapshot
    is terminal, and if the snapshot has been evicted (or was never written) a
    finished `jobs` row short-circuits into a single synthetic terminal event.

    Identity comes entirely from the ?token= stream token — deliberately NOT
    get_current_user, which reads the Authorization header EventSource cannot
    send.  The token was minted by issue_stream_token only after a tenant +
    ownership check, and is bound to this job_id (admins and support staff
    can mint for any same-tenant job; regular users only for their own).  The
    row lookup below re-uses the tenant from that token and stays tenant-scoped
    (`get_for_tenant`); it grants nothing the token did not already grant.
    """
    if token is None:
        raise AuthenticationError("Missing stream token")
    # Raises AuthenticationError (401) if invalid, expired, or not type=stream —
    # in particular, a primary access JWT pasted into the URL is refused.
    payload = decode_token(token, expected_type="stream")
    if payload.get("sub") != str(job_id):
        raise AuthorizationError("Stream token was not issued for this job")
    try:
        tenant_id = uuid.UUID(str(payload.get("tenant_id")))
    except ValueError as exc:
        raise AuthenticationError("Stream token carries no usable tenant") from exc

    # Give the read below the same RLS backstop every other authenticated
    # path has. This endpoint authenticates on the `?token=` stream token
    # rather than `get_current_user`, so nothing had set `app.tenant_id`
    # and the lookup ran unscoped — carried by the policy's bootstrap
    # branch until WO-R2-129 removed it. The tenant comes from the signed
    # token that was already checked against this job_id, so this narrows
    # the query, it does not widen it.
    await declare_tenant_scope(db, tenant_id)

    # Read the row here, not inside the generator: the get_db session is torn
    # down when this function returns, before a single SSE byte is streamed.
    job = await JobRepository(db).get_for_tenant(job_id, tenant_id)
    finished_event = (
        _terminal_event_from_row(job)
        if job is not None and job.status in TERMINAL_STATUSES
        else None
    )

    # Reserve the stream slot BEFORE returning a response: a refusal has to be
    # a normal 503 with Retry-After, not a stream that opens and then dies.
    # This raises StreamCapacityError and never touches Redis, so a full
    # process refuses cheaply instead of queueing against a finite pool.
    slot = acquire_stream_slot()

    row_updated_at = job.updated_at if job is not None else None

    async def _event_stream() -> AsyncGenerator[dict[str, str], None]:
        try:
            snapshot = await read_last_event(redis, str(job_id))
            if finished_event is not None and snapshot is None:
                # Job is over and Redis retained nothing to say so — the channel
                # would stay silent forever. Report the row and close.
                yield {"data": finished_event.to_json(), "event": finished_event.status}
                return
            # The other disagreement: a terminal snapshot in front of a row
            # that is not terminal, which is what a DLQ replay leaves behind
            # (the snapshot's own guard refuses to let the replay's `running`
            # events overwrite `dead_letter`). Handing that snapshot to the
            # client closes the stream on its first event, for a job that is
            # running right now. The row is durable state and it moved after
            # the snapshot was written, so the row wins and we stream live.
            stale = _snapshot_is_stale(
                snapshot,
                row_is_terminal=finished_event is not None,
                row_updated_at=row_updated_at,
            )
            async for event in subscribe(str(job_id), use_snapshot=not stale):
                yield {"data": event.to_json(), "event": event.status}
        finally:
            slot.release()

    # The background task is the belt to the generator's braces: if the client
    # disappears between here and the first byte the generator is never driven,
    # so its `finally` never runs and the slot would leak. `release()` is
    # idempotent, so whichever fires first wins and the other is a no-op.
    return EventSourceResponse(_event_stream(), background=BackgroundTask(slot.release))
