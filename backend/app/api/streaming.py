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

from app.core.exceptions import AuthenticationError, AuthorizationError
from app.core.redis import get_redis
from app.core.security import create_stream_token, decode_token
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

    async def _event_stream() -> AsyncGenerator[dict[str, str], None]:
        try:
            if finished_event is not None and await read_last_event(redis, str(job_id)) is None:
                # Job is over and Redis retained nothing to say so — the channel
                # would stay silent forever. Report the row and close.
                yield {"data": finished_event.to_json(), "event": finished_event.status}
                return
            async for event in subscribe(str(job_id)):
                yield {"data": event.to_json(), "event": event.status}
        finally:
            slot.release()

    # The background task is the belt to the generator's braces: if the client
    # disappears between here and the first byte the generator is never driven,
    # so its `finally` never runs and the slot would leak. `release()` is
    # idempotent, so whichever fires first wins and the other is a no-op.
    return EventSourceResponse(_event_stream(), background=BackgroundTask(slot.release))
