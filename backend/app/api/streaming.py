"""
Server-Sent Events endpoint for live job progress.

POST /api/v1/jobs/{job_id}/stream-token  — mint a short-lived stream token
GET  /api/v1/jobs/{job_id}/stream?token= — the SSE stream itself

The client opens the stream once and receives text/event-stream events as the
worker publishes progress.  The connection closes automatically when the job
reaches a terminal state (completed / failed / dead_letter).

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
"""

import uuid
from collections.abc import AsyncGenerator

from app.core.exceptions import AuthenticationError, AuthorizationError
from app.core.redis import get_redis
from app.core.security import create_stream_token, decode_token
from app.dependencies import get_current_user, get_db
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.job import JobRepository
from app.repositories.outbox import OutboxRepository
from app.schemas.job import StreamTokenResponse
from app.services.job import JobService
from app.workers.progress import subscribe
from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

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


@router.get("/jobs/{job_id}/stream")
async def stream_job_progress(
    job_id: uuid.UUID,
    token: str | None = Query(default=None),
    redis: Redis = Depends(get_redis),
) -> EventSourceResponse:
    """
    Stream live progress events for a job via Server-Sent Events.

    Events are JSON-encoded ProgressEvent objects:
      { job_id, status, progress, message, retry_count, timestamp }

    The stream closes when status is one of: completed | failed | dead_letter.

    Identity comes entirely from the ?token= stream token — deliberately NOT
    get_current_user, which reads the Authorization header EventSource cannot
    send.  The token was minted by issue_stream_token only after a tenant +
    ownership check, and is bound to this job_id (admins and support staff
    can mint for any same-tenant job; regular users only for their own).
    """
    if token is None:
        raise AuthenticationError("Missing stream token")
    # Raises AuthenticationError (401) if invalid, expired, or not type=stream —
    # in particular, a primary access JWT pasted into the URL is refused.
    payload = decode_token(token, expected_type="stream")
    if payload.get("sub") != str(job_id):
        raise AuthorizationError("Stream token was not issued for this job")

    async def _event_stream() -> AsyncGenerator[dict[str, str], None]:
        async for event in subscribe(redis, str(job_id)):
            yield {"data": event.to_json(), "event": event.status}

    return EventSourceResponse(_event_stream())
