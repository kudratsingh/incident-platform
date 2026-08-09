"""Exactly-once publish under two concurrent relay ticks on one Postgres.

This is the assertion the unit tier structurally cannot express. The
unit/API suites run on in-memory SQLite, which has no
`pg_try_advisory_lock` (and no notion of a second session contending for
one), so there the leader gate is a documented no-op and every unit test
has to *inject* leadership rather than compete for it. Whether two real
processes can both win the gate — and whether the lock survives the
relay's three transaction boundaries on a pooled async connection — is
only answerable against a real server.

Two tests, and the second is the control that makes the first mean
something:

  1. `test_two_concurrent_relays_publish_each_row_exactly_once` — ten
     unpublished rows, two relay ticks racing on one database. The two
     publish sets must be disjoint and their union must be all ten.
  2. `test_without_the_gate_both_relays_publish_the_whole_backlog` — the
     same race with the gate bypassed, which is exactly what HEAD did
     before this change: both relays publish all ten, twenty publishes
     total. This pins the defect as real rather than theoretical, and
     would fail if some unrelated mechanism (row locks, timing) were
     quietly doing the deduplication instead of the gate.

Skipped automatically when Docker / testcontainers isn't available so the
rest of the suite still runs.
"""

import asyncio
import contextvars
import subprocess
import uuid
from typing import Any
from unittest.mock import patch

import pytest
from app.core.leader_lock import OUTBOX_RELAY_LOCK_KEY, advisory_leader_lock
from app.models.base import Base
from app.models.outbox import OutboxEvent
from app.models.tenant import Tenant
from app.workers import dispatcher
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

try:
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    _HAS_TC = True
except Exception:  # pragma: no cover
    _HAS_TC = False


def _has_docker() -> bool:
    try:
        subprocess.run(
            ["docker", "info"], capture_output=True, timeout=30, check=True
        )
        return True
    except Exception:  # pragma: no cover - environment-dependent
        return False


pytestmark = pytest.mark.skipif(
    not _HAS_TC or not _has_docker(),
    reason="needs Docker + testcontainers[postgres]",
)

ROW_COUNT = 10

#: Which relay is publishing. Set inside each task; `asyncio.create_task`
#: copies the context, so the two relays see their own value.
_relay_id: contextvars.ContextVar[str] = contextvars.ContextVar("relay_id")


@pytest.fixture(scope="module")
def pg() -> Any:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


@pytest.fixture
async def session_factory(pg: Any) -> Any:
    """A factory over a real Postgres, with the outbox schema created.

    A pool with room for several connections on purpose: the gate checks
    out one of its own for the lock while the tick's sessions use others,
    which is the arrangement the connection-scoping trap lives in.
    """
    engine = create_async_engine(pg.get_connection_url(), pool_size=5, max_overflow=5)
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[Tenant.__table__, OutboxEvent.__table__],
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.drop_all,
            tables=[OutboxEvent.__table__, Tenant.__table__],
        )
    await engine.dispose()


async def _seed(factory: Any) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    # Own transaction: the outbox rows carry an FK to it.
    async with factory() as session:
        async with session.begin():
            session.add(
                Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[:8]}", name="relay test")
            )
    async with factory() as session:
        async with session.begin():
            for i in range(ROW_COUNT):
                session.add(
                    OutboxEvent(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        topic="job.submitted",
                        key=f"{tenant_id}:{i}",
                        payload={"event": "job.submitted", "n": i},
                    )
                )
    return tenant_id


def _recording_publisher(sink: dict[str, list[tuple[str, str, int]]]) -> Any:
    async def _publish_raw(*, topic: str, key: str, payload: dict[str, Any]) -> None:
        # A round-trip's worth of latency, so the two ticks genuinely
        # interleave instead of one finishing before the other starts.
        await asyncio.sleep(0.01)
        sink[_relay_id.get()].append((topic, key, payload["n"]))

    return _publish_raw


async def _run_relay(name: str, factory: Any, *, gated: bool) -> None:
    _relay_id.set(name)
    if not gated:
        await dispatcher._outbox_relay_tick(factory)
        return
    async with advisory_leader_lock(factory, OUTBOX_RELAY_LOCK_KEY) as is_leader:
        if is_leader:
            await dispatcher._outbox_relay_tick(factory)


async def _race(factory: Any, *, gated: bool) -> dict[str, list[tuple[str, str, int]]]:
    sink: dict[str, list[tuple[str, str, int]]] = {"a": [], "b": []}
    with patch(
        "app.workers.dispatcher.kafka_producer.publish_raw",
        new=_recording_publisher(sink),
    ):
        await asyncio.gather(
            _run_relay("a", factory, gated=gated),
            _run_relay("b", factory, gated=gated),
        )
    return sink


async def test_two_concurrent_relays_publish_each_row_exactly_once(
    session_factory: Any,
) -> None:
    await _seed(session_factory)

    sink = await _race(session_factory, gated=True)
    a, b = set(sink["a"]), set(sink["b"])

    assert not (a & b), f"both relays published {sorted(a & b)}"
    assert len(a | b) == ROW_COUNT
    assert len(sink["a"]) + len(sink["b"]) == ROW_COUNT, "a row published twice"

    async with session_factory() as session:
        unpublished = (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.published_at.is_(None))
            )
        ).scalars().all()
    assert unpublished == []


async def test_without_the_gate_both_relays_publish_the_whole_backlog(
    session_factory: Any,
) -> None:
    """The defect, reproduced: this is HEAD's behavior on every rolling
    deploy. Keep it — it is what proves the gate is load-bearing."""
    await _seed(session_factory)

    sink = await _race(session_factory, gated=False)

    assert len(sink["a"]) + len(sink["b"]) > ROW_COUNT
    assert set(sink["a"]) & set(sink["b"]), "expected the same rows published twice"


async def test_the_gate_lets_the_next_tick_in_after_the_first_releases(
    session_factory: Any,
) -> None:
    """Leadership is per tick, not sticky: the lock is released at the end
    of every tick so a surviving replica can take over after a deploy."""
    async with advisory_leader_lock(session_factory, OUTBOX_RELAY_LOCK_KEY) as first:
        assert first is True
        async with advisory_leader_lock(
            session_factory, OUTBOX_RELAY_LOCK_KEY
        ) as concurrent:
            assert concurrent is False, "a second holder won the same lock"

    async with advisory_leader_lock(session_factory, OUTBOX_RELAY_LOCK_KEY) as after:
        assert after is True, "the lock was never released"
