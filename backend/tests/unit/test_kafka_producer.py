"""Unit tests for the Kafka producer module.

The real AIOKafkaProducer is replaced with an AsyncMock so we can assert the
payload shape `publish_job_progress` and `publish_raw` send to the broker.

Historical note: this module also used to expose `publish_job_submitted`,
`publish_job_completed`, and `publish_job_failed` helpers. Those were
superseded by the transactional outbox + `publish_raw` and are now gone —
the only direct publish path left is `publish_job_progress` (high-frequency
progress events that don't need outbox durability).
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


async def test_publish_job_progress_payload(_mock_producer: AsyncMock) -> None:
    job_id = uuid.uuid4()
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    await kafka_producer.publish_job_progress(
        job_id=job_id,
        user_id=user_id,
        tenant_id=tenant_id,
        status="running",
        percent=42,
        message="halfway",
        retry_count=2,
    )

    kwargs = _kwargs(_mock_producer)
    settings = get_settings()
    assert _mock_producer.send_and_wait.await_args.args[0] == settings.kafka_topic_job_progress
    assert kwargs["key"] == str(user_id)
    value = kwargs["value"]
    assert value["tenant_id"] == str(tenant_id)
    assert value["status"] == "running"
    assert value["percent"] == 42
    assert value["retry_count"] == 2


async def test_publish_progress_swallows_broker_errors(
    _mock_producer: AsyncMock,
) -> None:
    """A failed send must not propagate — the worker can't be blocked by Kafka."""
    _mock_producer.send_and_wait.side_effect = RuntimeError("broker down")
    # Should not raise.
    await kafka_producer.publish_job_progress(
        job_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        status="running",
        percent=10,
        message="x",
    )


async def test_publish_raw_propagates_errors(_mock_producer: AsyncMock) -> None:
    """publish_raw (used by the outbox relay) must let errors propagate so the
    relay can leave the row unpublished."""
    _mock_producer.send_and_wait.side_effect = RuntimeError("broker down")
    with pytest.raises(RuntimeError, match="broker down"):
        await kafka_producer.publish_raw(
            topic="job.submitted",
            key="user-1",
            payload={
                "event": "job.submitted",
                "tenant_id": str(uuid.uuid4()),
                "job_id": str(uuid.uuid4()),
                "user_id": str(uuid.uuid4()),
                "job_type": "csv_upload",
            },
        )
