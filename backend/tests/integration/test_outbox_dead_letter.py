"""The outbox→relay→Kafka round-trip, and what happens to a row that can never make it.

ADR 0001's Verification section has cited an integration test named
`test_outbox_relay` since Phase 7. It never existed, so the pattern the ADR
is *about* — write outbox row, relay publishes, consumer reads it — had no
end-to-end proof anywhere in the repo. `test_relay_round_trip_reaches_a_real_consumer`
below is that proof, and the ADR now cites it by its real name.

The other three tests are the defect that absence hid (WO-R2-05). The relay
incremented an `attempts` counter that nothing read: no cap, no failed
state, no error column. A row that could never publish was therefore retried
every tick forever — and because `fetch_unpublished` returns a *fixed*
oldest-`OUTBOX_RELAY_BATCH` window, each such row permanently occupied one
of exactly 100 slots. That is not gradual degradation. It is a cliff: at 100
poison rows the window is full of them and no other event is ever fetched
again, for any tenant, with no error rate to see it by.

Both halves need a real server to mean anything:

  * Real Postgres, because the fix is partly a SQL predicate (`attempts <
    cap`) and partly a write (`published_at`/`failed_at`/`error_message`)
    against a real column set. SQLite would prove neither.
  * Real Redpanda, because "cannot publish" has to be genuine. The oversize
    row here is refused by the actual Kafka client for the actual reason a
    production row would be — not by a mock we told to raise.
"""

import asyncio
import json
import subprocess
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from aiokafka import AIOKafkaConsumer
from app.config import get_settings
from app.models.base import Base
from app.models.outbox import OutboxEvent
from app.models.tenant import Tenant
from app.repositories.outbox import OutboxRepository
from app.workers import dispatcher, kafka_producer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

try:
    import docker  # noqa: F401
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    _HAS_TC = True
except Exception:  # pragma: no cover - testcontainers or docker not installed
    _HAS_TC = False


def _docker_running() -> bool:
    if not _HAS_TC:
        return False
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=True)
        return True
    except Exception:  # pragma: no cover - environment-dependent
        return False


pytestmark = pytest.mark.skipif(
    not _docker_running(),
    reason="needs Docker + testcontainers[postgres]",
)

#: Low enough to drive a row through the cap in a handful of ticks. The
#: production default is ~900 (about fifteen minutes of continuous failure)
#: precisely so a broker outage does not quarantine a healthy backlog; a
#: test that had to tick 900 times would be asserting about patience.
TEST_MAX_ATTEMPTS = 3

#: Comfortably over aiokafka's 1 MiB `max_request_size`. The client refuses
#: this before it reaches the broker, identically on every retry — which is
#: exactly what makes it poison rather than a transient failure.
_OVERSIZE_BYTES = 1_500_000

TOPIC = "job.submitted"


def _find_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def pg() -> Any:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


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


@pytest_asyncio.fixture
async def relay(
    pg: Any, redpanda: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[Any, None]:
    """A session factory over real Postgres, with the real producer pointed at
    real Redpanda and the attempt cap lowered.

    Yields the factory. The producer is a module-level singleton, so it is
    started and stopped here rather than leaking into the next test.
    """
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", redpanda)
    monkeypatch.setenv("OUTBOX_MAX_ATTEMPTS", str(TEST_MAX_ATTEMPTS))
    monkeypatch.setenv("CHAOS_ENABLED", "false")
    get_settings.cache_clear()

    engine = create_async_engine(pg.get_connection_url(), pool_size=5, max_overflow=5)
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[Tenant.__table__, OutboxEvent.__table__],
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    await kafka_producer.start_producer()
    try:
        yield factory
    finally:
        await kafka_producer.stop_producer()
        async with engine.begin() as conn:
            await conn.run_sync(
                Base.metadata.drop_all,
                tables=[OutboxEvent.__table__, Tenant.__table__],
            )
        await engine.dispose()
        get_settings.cache_clear()


async def _seed_tenant(factory: Any) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            session.add(
                Tenant(id=tenant_id, name=f"t-{tenant_id.hex[:8]}", slug=tenant_id.hex[:12])
            )
    return tenant_id


def _valid_payload(tenant_id: uuid.UUID) -> dict[str, Any]:
    return {
        "event": "job.submitted",
        "tenant_id": str(tenant_id),
        "job_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "job_type": "csv_upload",
        "payload": {"file": "x.csv"},
        "priority": 0,
        "trace_id": f"t-{uuid.uuid4().hex[:8]}",
    }


def _oversize_payload(tenant_id: uuid.UUID) -> dict[str, Any]:
    """Schema-valid but far too large for the broker to accept.

    Deliberately not schema-invalid: that path dead-letters on the first
    attempt and would never exercise the cap. This row is well-formed and
    still impossible, which is the case the counter was supposed to catch.
    """
    payload = _valid_payload(tenant_id)
    payload["payload"] = {"blob": "x" * _OVERSIZE_BYTES}
    return payload


async def _add_row(factory: Any, tenant_id: uuid.UUID, payload: dict[str, Any]) -> uuid.UUID:
    async with factory() as session:
        async with session.begin():
            row = await OutboxRepository(session).add(
                tenant_id=tenant_id,
                topic=TOPIC,
                # The malformed payloads below have no user_id; the
                # partition key is not what is under test here.
                key=f"{tenant_id}:{payload.get('user_id', 'poison')}",
                payload=payload,
            )
            return row.id


async def _load(factory: Any, row_id: uuid.UUID) -> OutboxEvent:
    async with factory() as session:
        result = await session.execute(
            select(OutboxEvent).where(OutboxEvent.id == row_id)
        )
        return result.scalar_one()


async def _fetch_window(factory: Any) -> list[uuid.UUID]:
    """Exactly what the next tick would pick up."""
    async with factory() as session:
        async with session.begin():
            rows = await OutboxRepository(session).fetch_unpublished(
                limit=dispatcher.OUTBOX_RELAY_BATCH
            )
            return [r.id for r in rows]


async def test_relay_round_trip_reaches_a_real_consumer(relay: Any) -> None:
    """Write an outbox row → relay tick → a consumer reads it off Kafka.

    The test ADR 0001 has cited since Phase 7 and never had.
    """
    tenant_id = await _seed_tenant(relay)
    payload = _valid_payload(tenant_id)
    row_id = await _add_row(relay, tenant_id, payload)

    consumer = AIOKafkaConsumer(
        TOPIC,
        bootstrap_servers=get_settings().kafka_bootstrap_servers,
        group_id=f"outbox-roundtrip-{uuid.uuid4().hex[:8]}",
        auto_offset_reset="earliest",
        value_deserializer=lambda v: json.loads(v.decode()),
    )
    await consumer.start()
    try:
        await dispatcher._outbox_relay_tick(relay)

        received = await asyncio.wait_for(consumer.getone(), timeout=30)
    finally:
        await consumer.stop()

    assert received.value["job_id"] == payload["job_id"]

    row = await _load(relay, row_id)
    assert row.published_at is not None, "a delivered row must be marked published"
    assert row.failed_at is None, "a delivered row must not look dead-lettered"
    assert row.error_message is None


async def test_an_unpublishable_row_is_dead_lettered_at_the_cap(relay: Any) -> None:
    """The row that used to be retried forever.

    Before the cap existed this loop ran unbounded: every tick incremented
    `attempts`, nothing ever read it, and the row stayed in the fetch window
    for the lifetime of the deployment.
    """
    tenant_id = await _seed_tenant(relay)
    row_id = await _add_row(relay, tenant_id, _oversize_payload(tenant_id))

    # One tick short of the cap: still queued, still counting.
    for _ in range(TEST_MAX_ATTEMPTS - 1):
        await dispatcher._outbox_relay_tick(relay)

    row = await _load(relay, row_id)
    assert row.failed_at is None, "must not give up early — a blip is not poison"
    assert row.attempts == TEST_MAX_ATTEMPTS - 1
    assert row_id in await _fetch_window(relay)

    # The tick that reaches the cap.
    await dispatcher._outbox_relay_tick(relay)

    row = await _load(relay, row_id)
    assert row.failed_at is not None, "row past the cap must be dead-lettered"
    assert row.published_at is not None, "dead-lettered rows leave the queue (ADR 0001)"
    assert row.error_message is not None
    assert f"abandoned after {TEST_MAX_ATTEMPTS} attempts" in row.error_message

    # The point of all of it: the row is out of the relay's way for good.
    assert row_id not in await _fetch_window(relay)

    # And the payload is still there to requeue from — quarantine, not deletion.
    assert row.payload["job_type"] == "csv_upload"

    # A further tick has nothing to do and must not resurrect it.
    await dispatcher._outbox_relay_tick(relay)
    assert row_id not in await _fetch_window(relay)


async def test_a_healthy_row_behind_a_full_window_of_poison_still_publishes(
    relay: Any,
) -> None:
    """The cliff, reproduced at its exact edge.

    `OUTBOX_RELAY_BATCH` poison rows, all older than one healthy row. Without
    a dead-letter exit the window is 100/100 poison on every tick and the
    healthy row is never even fetched — not slowly, not eventually, never.
    """
    tenant_id = await _seed_tenant(relay)

    # Schema-invalid rather than oversize: same permanent unpublishability,
    # a hundredth of the bytes. These take the immediate dead-letter branch.
    for _ in range(dispatcher.OUTBOX_RELAY_BATCH):
        await _add_row(relay, tenant_id, {"event": "job.submitted", "nonsense": True})

    healthy_payload = _valid_payload(tenant_id)
    healthy_id = await _add_row(relay, tenant_id, healthy_payload)

    # It starts out exactly as the finding describes: shut out of the window.
    window = await _fetch_window(relay)
    assert len(window) == dispatcher.OUTBOX_RELAY_BATCH
    assert healthy_id not in window

    # First tick clears the poison, second publishes the row behind it.
    await dispatcher._outbox_relay_tick(relay)
    assert healthy_id in await _fetch_window(relay)
    await dispatcher._outbox_relay_tick(relay)

    row = await _load(relay, healthy_id)
    assert row.published_at is not None, "healthy row must not be starved by poison"
    assert row.failed_at is None
    assert row.error_message is None


async def test_unpublished_stats_sees_a_stall_that_queue_depth_cannot(
    relay: Any,
) -> None:
    """The gauge behind the stall alarm, measured against real rows.

    QueueDepth reads the Redis delayed set and stays green through a total
    outbox stall, so these two numbers are the only warning anyone gets.
    """
    tenant_id = await _seed_tenant(relay)

    async with relay() as session:
        depth, age = await OutboxRepository(session).unpublished_stats()
    assert (depth, age) == (0, 0.0), "an empty outbox must report zero, not None"

    await _add_row(relay, tenant_id, _oversize_payload(tenant_id))
    await _add_row(relay, tenant_id, _valid_payload(tenant_id))

    async with relay() as session:
        depth, age = await OutboxRepository(session).unpublished_stats()
    assert depth == 2
    assert age >= 0.0
