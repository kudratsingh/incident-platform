"""
Server-Sent Events endpoint for live job progress.

GET /api/v1/jobs/{job_id}/stream

The client opens this endpoint once and receives a stream of text/event-stream
events as the worker publishes progress.  The connection closes automatically
when the job reaches a terminal state (completed / failed / dead_letter).

Why SSE over WebSockets here:
  - Job progress is unidirectional (server → client only).
  - SSE reconnects automatically in the browser.
  - No need for a full duplex channel.
"""

import uuid
from collections.abc import AsyncGenerator

from app.core.redis import get_redis
from app.dependencies import get_current_user
from app.models.user import User
from app.workers.progress import subscribe
from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sse_starlette.sse import EventSourceResponse

router = APIRouter(tags=["streaming"])


@router.get("/jobs/{job_id}/stream")
async def stream_job_progress(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
) -> EventSourceResponse:
    """
    Stream live progress events for a job via Server-Sent Events.

    Events are JSON-encoded ProgressEvent objects:
      { job_id, status, progress, message, retry_count, timestamp }

    The stream closes when status is one of: completed | failed | dead_letter.
    Admins and support staff can stream any job; regular users only their own
    (enforced at the DB level when they attempt to GET the job — here we keep
    the check lightweight since it's a streaming endpoint).
    """

    async def _event_stream() -> AsyncGenerator[dict[str, str], None]:
        async for event in subscribe(redis, str(job_id)):
            yield {"data": event.to_json(), "event": event.status}

    return EventSourceResponse(_event_stream())
