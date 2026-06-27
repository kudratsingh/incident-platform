"""Unit tests for SseConsumer — Redis pub/sub fully mocked."""

import uuid
from unittest.mock import AsyncMock, patch

from app.workers.sse_consumer import SseConsumer


async def test_sse_progress_event_published_to_channel() -> None:
    redis = AsyncMock()
    consumer = SseConsumer(redis)
    job_id = uuid.uuid4()

    with patch("app.workers.sse_consumer.publish_progress", new=AsyncMock()) as mock_pub:
        await consumer.handle_message(
            topic="job.progress",
            key="u",
            value={
                "event": "job.progress",
                "job_id": str(job_id),
                "status": "running",
                "percent": 42,
                "message": "halfway",
                "retry_count": 1,
            },
        )

    mock_pub.assert_awaited_once()
    kwargs = mock_pub.await_args.kwargs
    assert kwargs["status"] == "running"
    assert kwargs["progress"] == 42
    assert kwargs["message"] == "halfway"
    assert kwargs["retry_count"] == 1
    assert kwargs["job_id"] == str(job_id)


async def test_sse_completed_event_maps_to_completed_status() -> None:
    redis = AsyncMock()
    consumer = SseConsumer(redis)

    with patch("app.workers.sse_consumer.publish_progress", new=AsyncMock()) as mock_pub:
        await consumer.handle_message(
            topic="job.completed",
            key="u",
            value={
                "event": "job.completed",
                "job_id": str(uuid.uuid4()),
                "result": {"rows": 5},
            },
        )

    kwargs = mock_pub.await_args.kwargs
    assert kwargs["status"] == "completed"
    assert kwargs["progress"] == 100


async def test_sse_failed_non_dlq_maps_to_retrying() -> None:
    redis = AsyncMock()
    consumer = SseConsumer(redis)

    with patch("app.workers.sse_consumer.publish_progress", new=AsyncMock()) as mock_pub:
        await consumer.handle_message(
            topic="job.failed",
            key="u",
            value={
                "event": "job.failed",
                "job_id": str(uuid.uuid4()),
                "dead_lettered": False,
                "error": "transient",
                "message": "Retrying in 4s (attempt 2/3)",
                "retry_count": 2,
            },
        )

    kwargs = mock_pub.await_args.kwargs
    assert kwargs["status"] == "retrying"
    assert kwargs["message"] == "Retrying in 4s (attempt 2/3)"
    assert kwargs["retry_count"] == 2


async def test_sse_failed_dlq_maps_to_dead_letter() -> None:
    redis = AsyncMock()
    consumer = SseConsumer(redis)

    with patch("app.workers.sse_consumer.publish_progress", new=AsyncMock()) as mock_pub:
        await consumer.handle_message(
            topic="job.dlq",
            key="u",
            value={
                "event": "job.failed",
                "job_id": str(uuid.uuid4()),
                "dead_lettered": True,
                "error": "exhausted",
                "message": "Job exhausted after 3 attempts: boom",
            },
        )

    kwargs = mock_pub.await_args.kwargs
    assert kwargs["status"] == "dead_letter"


async def test_sse_skips_malformed_event() -> None:
    redis = AsyncMock()
    consumer = SseConsumer(redis)

    with patch("app.workers.sse_consumer.publish_progress", new=AsyncMock()) as mock_pub:
        await consumer.handle_message(
            topic="job.progress", key="u", value={"missing": "fields"}
        )

    mock_pub.assert_not_awaited()
