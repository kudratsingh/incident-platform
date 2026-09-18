"""
Server-Sent Events endpoint for live job progress: `POST
/jobs/{job_id}/stream-token` mints a job-bound token, `GET
/jobs/{job_id}/stream?token=` streams it.  EventSource cannot set headers, so
auth is a token in the query string (ADR 0014).  Every stream shares one
Pub/Sub connection on a streaming-only Redis pool, capped (WO-R2-11).
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

    Where the stream is authorized: `get_job` 404s a cross-tenant job and 403s
    a non-owner.  The token's subject is the job id, so it cannot be replayed.
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

    Built from the dataclass, never hand-rolled JSON, so it matches a
    published event on the wire.
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

    A DLQ replay is how that happens: the replay's `running` events never
    overwrite a terminal snapshot. The tie-break is recency — the row wins
    only if it was written after the snapshot (WO-R2-57).
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

    Closes on completed | failed | dead_letter | cancelled, late subscribers
    included (the retained `job:progress:last:{job_id}` snapshot, else one
    synthetic terminal event).  Identity is the ?token= token (ADR 0014).
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

    # Give this read the RLS backstop every other authenticated path has —
    # nothing had set `app.tenant_id` here (WO-R2-129). The tenant comes from
    # the token already checked against this job_id, so it only narrows.
    await declare_tenant_scope(db, tenant_id)

    # Read the row here, not inside the generator: the get_db session is torn
    # down when this function returns, before a single SSE byte is streamed.
    job = await JobRepository(db).get_for_tenant(job_id, tenant_id)
    finished_event = (
        _terminal_event_from_row(job)
        if job is not None and job.status in TERMINAL_STATUSES
        else None
    )

    # Reserve the slot BEFORE responding: a refusal must be a 503 with
    # Retry-After, not a stream that opens and dies. Touches no Redis.
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
            # The other disagreement: a terminal snapshot in front of a
            # non-terminal row, which a DLQ replay leaves behind. Serving it
            # would close the stream on a running job, so the row wins.
            stale = _snapshot_is_stale(
                snapshot,
                row_is_terminal=finished_event is not None,
                row_updated_at=row_updated_at,
            )
            async for event in subscribe(str(job_id), use_snapshot=not stale):
                yield {"data": event.to_json(), "event": event.status}
        finally:
            slot.release()

    # Belt to the generator's braces: a client that vanishes before the first
    # byte never drives it, so the slot would leak. `release()` is idempotent.
    return EventSourceResponse(_event_stream(), background=BackgroundTask(slot.release))
