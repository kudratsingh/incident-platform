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
from aiokafka.errors import KafkaConnectionError  # type: ignore[import-untyped]
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
    # Partition key is now composite: tenant_id first, user_id second.
    assert kwargs["key"] == f"{tenant_id}:{user_id}"
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


# --------------------------------------------------------------------------
# Producer lifecycle / self-heal after a boot-time start failure
# --------------------------------------------------------------------------


class _DownProducer:
    """Stub whose start() fails the way an unreachable broker does."""

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def start(self) -> None:
        raise KafkaConnectionError("no brokers available")


class _UpProducer:
    """Stub whose start() succeeds; send_and_wait is an AsyncMock."""

    def __init__(self, **_kwargs: Any) -> None:
        self.started = False
        self.send_and_wait = AsyncMock()

    async def start(self) -> None:
        self.started = True


def _reset_producer_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the autouse AsyncMock fixture and defeat the start-retry throttle."""
    monkeypatch.setattr(kafka_producer, "_producer", None)
    monkeypatch.setattr(kafka_producer, "_last_start_attempt", 0.0, raising=False)


def _submitted_payload() -> dict[str, Any]:
    return {
        "event": "job.submitted",
        "tenant_id": str(uuid.uuid4()),
        "job_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "job_type": "csv_upload",
    }


async def test_failed_start_leaves_producer_unassigned(monkeypatch: pytest.MonkeyPatch) -> None:
    """A start() failure must leave the singleton as None, not as a broken,
    un-started instance — otherwise every lazy-restart path is dead on arrival
    and _get_producer() hands out an unusable producer."""
    _reset_producer_state(monkeypatch)
    monkeypatch.setattr(kafka_producer, "AIOKafkaProducer", _DownProducer)

    with pytest.raises(KafkaConnectionError):
        await kafka_producer.start_producer()

    assert kafka_producer._producer is None


async def test_publish_raw_self_heals_after_failed_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a boot-time start failure, a later publish must restart the
    producer once the broker is back — no process restart required."""
    _reset_producer_state(monkeypatch)
    monkeypatch.setattr(kafka_producer, "AIOKafkaProducer", _DownProducer)

    with pytest.raises(KafkaConnectionError):
        await kafka_producer.start_producer()

    # Broker recovers. Clear the throttle stamp so the next publish retries now.
    recovered = _UpProducer()
    monkeypatch.setattr(kafka_producer, "AIOKafkaProducer", lambda **_kw: recovered)
    monkeypatch.setattr(kafka_producer, "_producer", None)
    monkeypatch.setattr(kafka_producer, "_last_start_attempt", 0.0, raising=False)

    await kafka_producer.publish_raw(
        topic="job.submitted", key="user-1", payload=_submitted_payload()
    )

    assert recovered.started is True
    recovered.send_and_wait.assert_awaited_once()


async def test_publish_raw_throttles_start_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """While the broker is down, back-to-back publishes inside the retry window
    must pay only one connection attempt."""
    _reset_producer_state(monkeypatch)
    built: list[dict[str, Any]] = []

    def _factory(**kwargs: Any) -> _DownProducer:
        built.append(kwargs)
        return _DownProducer()

    monkeypatch.setattr(kafka_producer, "AIOKafkaProducer", _factory)

    with pytest.raises(KafkaConnectionError):
        await kafka_producer.publish_raw(
            topic="job.submitted", key="user-1", payload=_submitted_payload()
        )
    with pytest.raises(RuntimeError, match="failed recently"):
        await kafka_producer.publish_raw(
            topic="job.submitted", key="user-1", payload=_submitted_payload()
        )

    assert len(built) == 1
