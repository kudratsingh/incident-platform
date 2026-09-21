"""The Family B contrast, on a real Postgres: the outbox grows, the lag does not.

`kill_consumer('worker-dispatcher')` and `pause_control_loop('outbox_relay')` look the same
from above: Kafka backlog with climbing lag, versus unpublished `outbox_events` rows with
flat lag. Both are asserted, or it is not a contrast. Needs the leader gate (ADR 0020).
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.config import Settings
from app.models.base import Base
from app.models.outbox import OutboxEvent
from app.models.tenant import Tenant
from app.repositories.outbox import OutboxRepository
from app.workers import dispatcher
from app.workers.control_loop_pause import ControlLoopName, pause_key_for
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

_PAUSE_KEY = pause_key_for(ControlLoopName.OUTBOX_RELAY)
_LAG_KEY = dispatcher.BACKPRESSURE_LAG_KEY

#: Deliberately non-zero: zero could not be told apart from an absent reading.
_FLAT_LAG = 7


@pytest.fixture(scope="module")
def pg() -> Any:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


@pytest.fixture
async def session_factory(pg: Any) -> Any:
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


class _Redis:
    """The two keys this world needs, with a `set`/`get`/`delete` surface.

    A stub, not a container: one GET for the pause and one SET for the lag.
    """

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self._store[key] = str(value)
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self._store:
                del self._store[key]
                removed += 1
        return removed


class _Gate:
    """Leadership, taken and released per tick the way the real gate does."""

    def __init__(self, entries: list[bool]) -> None:
        self._entries = entries

    async def __aenter__(self) -> bool:
        self._entries.append(True)
        return True

    async def __aexit__(self, *exc: Any) -> None:
        return None


async def _make_tenant(factory: Any) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            session.add(
                Tenant(
                    id=tenant_id,
                    slug=f"t-{tenant_id.hex[:8]}",
                    name="outbox stall",
                )
            )
    return tenant_id


async def _submit(factory: Any, tenant_id: uuid.UUID, count: int) -> None:
    """What `POST /jobs` leaves behind: unpublished outbox rows."""
    async with factory() as session:
        async with session.begin():
            for _ in range(count):
                session.add(
                    OutboxEvent(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        topic="job.submitted",
                        key=f"{tenant_id}:{uuid.uuid4()}",
                        payload={"event": "job.submitted"},
                    )
                )


async def _outbox_status(
    factory: Any, tenant_id: uuid.UUID
) -> tuple[int, float | None]:
    """`unpublished_count` and `oldest_unpublished_age_s`, from the query
    `get_outbox_status` runs (WP-4.2), on the server whose clock it reads."""
    async with factory() as session:
        snapshot = await OutboxRepository(session).delivery_snapshot(
            tenant_id=tenant_id
        )
    if snapshot.oldest_unpublished_at is None:
        return snapshot.unpublished_count, None
    return snapshot.unpublished_count, (
        snapshot.measured_at - snapshot.oldest_unpublished_at
    ).total_seconds()


#: Shortened loop intervals (the real ones are 1 s and 60 s) and the window length.
#: Patching the constants leaves the loop bodies alone.
_FAST_INTERVAL = 0.02
_WINDOW_SECONDS = 0.4


async def _run_ticks(factory: Any, redis: _Redis, consumer: Any) -> list[bool]:
    """Let the real loops run for a short window, then cancel them. Cancellation,
    not a patched `asyncio.sleep`, because the relay holds a live asyncpg
    connection; `CHAOS_ENABLED=true` is gate 1 for the pause check."""
    entries: list[bool] = []

    async def _publish_raw(*, topic: str, key: str, payload: dict[str, Any]) -> None:
        return None

    with patch(
        "app.workers.control_loop_pause.get_settings",
        return_value=Settings(chaos_enabled=True, environment="test"),
    ), patch("app.core.redis.get_redis_client", return_value=redis), patch(
        "app.workers.dispatcher.kafka_producer.publish_raw", new=_publish_raw
    ), patch(
        "app.workers.dispatcher.metrics.emit_gauge", new=AsyncMock()
    ), patch(
        "app.workers.dispatcher.queue.delayed_length", new=AsyncMock(return_value=0)
    ), patch.object(
        dispatcher, "OUTBOX_RELAY_INTERVAL", _FAST_INTERVAL
    ), patch.object(
        dispatcher, "metrics_interval_seconds", lambda *_: _FAST_INTERVAL
    ):
        tasks = [
            asyncio.create_task(
                dispatcher._outbox_relay_loop(
                    factory, leader_gate=lambda: _Gate(entries)
                )
            ),
            asyncio.create_task(dispatcher._metrics_loop(redis, consumer)),
        ]
        await asyncio.sleep(_WINDOW_SECONDS)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    return entries


def _flat_lag_consumer() -> Any:
    """A dispatcher consumer whose lag does not move — the world a stopped relay
    leaves, where a climbing lag would be modelling the other fault."""
    consumer = AsyncMock()
    consumer.consumer_lag = AsyncMock(return_value=_FLAT_LAG)
    consumer.in_flight = set()
    return consumer


async def test_a_paused_relay_grows_the_outbox_while_dispatcher_lag_stays_flat(
    session_factory: Any,
) -> None:
    """The contrast on both signals: after the ticks every row is still
    unpublished, the oldest has aged, and the cached lag is flat and present."""
    tenant_id = await _make_tenant(session_factory)
    redis = _Redis()

    await _submit(session_factory, tenant_id, 4)
    before_count, before_age = await _outbox_status(session_factory, tenant_id)
    assert before_count == 4
    assert before_age is not None

    await redis.set(_PAUSE_KEY, "paused", ex=300)
    await asyncio.sleep(1.1)  # so "the oldest row aged" is measurable
    await _submit(session_factory, tenant_id, 3)

    entries = await _run_ticks(session_factory, redis, _flat_lag_consumer())

    after_count, after_age = await _outbox_status(session_factory, tenant_id)

    # Signal 1 — the outbox backlog grew and aged.
    assert after_count == 7, "the paused relay published rows anyway"
    assert after_age is not None and after_age > before_age, (
        "the oldest unpublished row did not age"
    )

    # Signal 2 — dispatcher lag is flat, and present. A missing reading would
    # let the fault be mistaken for a dead metrics loop.
    assert await redis.get(_LAG_KEY) == str(_FLAT_LAG)

    # And the pause did not cost the relay its leadership: it took the gate on
    # every tick it ran (ADR 0020, ADR 0027).
    assert len(entries) >= 3


async def test_the_relay_drains_the_backlog_once_the_pause_expires(
    session_factory: Any,
) -> None:
    """TTL restores publishing with no manual step. The key is deleted instead of
    waited out; to a GET the two are the same, and nothing calls a compensator."""
    tenant_id = await _make_tenant(session_factory)
    redis = _Redis()

    await _submit(session_factory, tenant_id, 5)
    await redis.set(_PAUSE_KEY, "paused", ex=300)

    await _run_ticks(session_factory, redis, _flat_lag_consumer())
    assert (await _outbox_status(session_factory, tenant_id))[0] == 5

    await redis.delete(_PAUSE_KEY)  # what the TTL does, on its own clock

    await _run_ticks(session_factory, redis, _flat_lag_consumer())
    assert (await _outbox_status(session_factory, tenant_id))[0] == 0, (
        "the relay did not resume on its own after the flag went away"
    )


async def test_pausing_a_different_loop_leaves_the_relay_publishing(
    session_factory: Any,
) -> None:
    """`single_loop` is a blast-radius claim: one member's key must not stop
    another's loop, as a shared key or prefix match would."""
    tenant_id = await _make_tenant(session_factory)
    redis = _Redis()

    await _submit(session_factory, tenant_id, 3)
    await redis.set(
        pause_key_for(ControlLoopName.STALE_RUNNING_SWEEP), "paused", ex=300
    )

    await _run_ticks(session_factory, redis, _flat_lag_consumer())

    assert (await _outbox_status(session_factory, tenant_id))[0] == 0, (
        "pausing another loop stopped the outbox relay"
    )

