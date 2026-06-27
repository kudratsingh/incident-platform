"""
End-to-end Kafka integration test.

Spins up Redpanda (Kafka API-compatible) via Testcontainers, then produces
a job.submitted message and asserts:
  1. The producer's schema validation accepts a valid payload.
  2. The consumer reads back the same payload.
  3. An invalid payload is rejected at produce time (schema validation).

Skipped automatically if Docker isn't reachable, so CI without Docker still
runs the unit/api suites.

Run only this file:  pytest backend/tests/integration/test_kafka_e2e.py -v
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from app.workers.schema_registry import SchemaValidationError
from app.workers.schema_registry import validate as validate_schema

try:
    import docker  # noqa: F401
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    _DOCKER_AVAILABLE = True
except Exception:  # pragma: no cover - testcontainers or docker not installed
    _DOCKER_AVAILABLE = False


def _docker_running() -> bool:
    if not _DOCKER_AVAILABLE:
        return False
    try:
        docker.from_env().ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _docker_running(),
    reason="Docker is not running; skipping Kafka integration tests.",
)


def _find_free_port() -> int:
    """Find a free TCP port to advertise Redpanda on. Kafka clients need the
    advertised address to match what they connect to, so we pin a known port
    before starting the container rather than letting Docker assign one."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


@pytest_asyncio.fixture(scope="module")
async def redpanda() -> AsyncGenerator[str, None]:
    """Start Redpanda and yield its bootstrap_servers string."""
    host_port = _find_free_port()
    container = (
        DockerContainer("redpandadata/redpanda:v24.1.7")
        .with_command(
            "redpanda start "
            "--smp 1 --memory 512M --reserve-memory 0M "
            "--overprovisioned --node-id 0 --check=false "
            f"--kafka-addr PLAINTEXT://0.0.0.0:{host_port} "
            f"--advertise-kafka-addr PLAINTEXT://localhost:{host_port}"
        )
        .with_bind_ports(host_port, host_port)
    )
    container.start()
    try:
        wait_for_logs(container, "Successfully started Redpanda!", timeout=60)
        yield f"localhost:{host_port}"
    finally:
        container.stop()


async def _await_until(predicate, timeout: float = 10.0, interval: float = 0.2) -> None:  # type: ignore[no-untyped-def]
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise TimeoutError("condition not met within timeout")


async def test_producer_consumer_round_trip(redpanda: str) -> None:
    """A valid job.submitted message reaches a consumer in the same group."""
    topic = "job.submitted"
    user_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    payload = {
        "event": "job.submitted",
        "job_id": job_id,
        "user_id": user_id,
        "job_type": "csv_upload",
        "payload": {"file": "x.csv"},
        "priority": 0,
        "trace_id": "t-abc",
    }

    # Validate before send — this is what the real producer does.
    validate_schema(topic, payload)

    producer = AIOKafkaProducer(
        bootstrap_servers=redpanda,
        value_serializer=lambda v: json.dumps(v).encode(),
        key_serializer=lambda k: k.encode(),
    )
    await producer.start()
    try:
        await producer.send_and_wait(topic, value=payload, key=user_id)
    finally:
        await producer.stop()

    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=redpanda,
        group_id=f"test-{uuid.uuid4()}",
        auto_offset_reset="earliest",
        value_deserializer=lambda v: json.loads(v.decode()),
    )
    await consumer.start()
    try:
        records = await asyncio.wait_for(
            consumer.getmany(timeout_ms=5000, max_records=1), timeout=10.0
        )
        messages = [m for batch in records.values() for m in batch]
        assert len(messages) == 1
        received = messages[0].value
        assert received["job_id"] == job_id
        assert received["user_id"] == user_id
        # Schema also validates on the consumer side.
        validate_schema(topic, received)
    finally:
        await consumer.stop()


async def test_producer_rejects_invalid_schema() -> None:
    """publish_raw's validation rejects payloads that violate the contract."""
    bad = {
        "event": "job.submitted",
        "job_id": "not-a-uuid",  # fails format: uuid
        "user_id": str(uuid.uuid4()),
        "job_type": "csv_upload",
    }
    with pytest.raises(SchemaValidationError):
        validate_schema("job.submitted", bad)
