"""Unit tests for the Kafka producer module.

The real AIOKafkaProducer is replaced with an AsyncMock so we can assert what
the publish_* helpers send to each topic (key partitioning, payload shape).
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.config import get_settings
from app.workers import kafka_producer


@pytest.fixture(autouse=True)
def _mock_producer(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace the module-level _producer with an AsyncMock for every test."""
    mock = AsyncMock()
    monkeypatch.setattr(kafka_producer, "_producer", mock)
    return mock


def _kwargs(mock: AsyncMock) -> dict[str, Any]:
    mock.send_and_wait.assert_awaited_once()
    return mock.send_and_wait.await_args.kwargs


async def test_publish_job_submitted_keys_by_user_id(_mock_producer: AsyncMock) -> None:
    job_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await kafka_producer.publish_job_submitted(
        job_id=job_id,
        user_id=user_id,
        job_type="csv_upload",
        payload={"file": "x.csv"},
        priority=1,
        trace_id="trace-abc",
    )

    kwargs = _kwargs(_mock_producer)
    settings = get_settings()
    assert _mock_producer.send_and_wait.await_args.args[0] == settings.kafka_topic_job_submitted
    assert kwargs["key"] == str(user_id)
    assert kwargs["value"] == {
        "event": "job.submitted",
        "job_id": str(job_id),
        "user_id": str(user_id),
        "job_type": "csv_upload",
        "payload": {"file": "x.csv"},
        "priority": 1,
        "trace_id": "trace-abc",
    }


async def test_publish_job_progress_payload(_mock_producer: AsyncMock) -> None:
    job_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await kafka_producer.publish_job_progress(
        job_id=job_id,
        user_id=user_id,
        status="running",
        percent=42,
        message="halfway",
        retry_count=2,
    )

    kwargs = _kwargs(_mock_producer)
    settings = get_settings()
    assert _mock_producer.send_and_wait.await_args.args[0] == settings.kafka_topic_job_progress
    assert kwargs["key"] == str(user_id)
    assert kwargs["value"]["status"] == "running"
    assert kwargs["value"]["percent"] == 42
    assert kwargs["value"]["retry_count"] == 2


async def test_publish_job_completed_payload(_mock_producer: AsyncMock) -> None:
    job_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await kafka_producer.publish_job_completed(
        job_id=job_id,
        user_id=user_id,
        job_type="report_gen",
        result={"rows": 100},
        retry_count=0,
    )

    kwargs = _kwargs(_mock_producer)
    settings = get_settings()
    assert _mock_producer.send_and_wait.await_args.args[0] == settings.kafka_topic_job_completed
    assert kwargs["value"]["result"] == {"rows": 100}


async def test_publish_job_failed_routes_to_failed_topic(_mock_producer: AsyncMock) -> None:
    await kafka_producer.publish_job_failed(
        job_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        job_type="bulk_api_sync",
        error="timeout",
        retry_count=1,
        dead_lettered=False,
    )
    settings = get_settings()
    assert _mock_producer.send_and_wait.await_args.args[0] == settings.kafka_topic_job_failed
    assert _mock_producer.send_and_wait.await_args.kwargs["value"]["dead_lettered"] is False


async def test_publish_job_failed_routes_to_dlq_when_dead_lettered(
    _mock_producer: AsyncMock,
) -> None:
    await kafka_producer.publish_job_failed(
        job_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        job_type="bulk_api_sync",
        error="exhausted",
        retry_count=3,
        dead_lettered=True,
    )
    settings = get_settings()
    assert _mock_producer.send_and_wait.await_args.args[0] == settings.kafka_topic_job_dlq
    assert _mock_producer.send_and_wait.await_args.kwargs["value"]["dead_lettered"] is True


async def test_publish_swallows_broker_errors(
    monkeypatch: pytest.MonkeyPatch, _mock_producer: AsyncMock
) -> None:
    """A failed send must not propagate — the API/worker can't be blocked by Kafka."""
    _mock_producer.send_and_wait.side_effect = RuntimeError("broker down")
    # Must not raise.
    await kafka_producer.publish_job_progress(
        job_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="running",
        percent=0,
        message="x",
    )
