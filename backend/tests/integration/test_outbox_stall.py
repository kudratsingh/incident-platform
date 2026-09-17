"""The Family B contrast, on a real Postgres: the outbox grows, the lag does not.

`kill_consumer('worker-dispatcher')` and `pause_control_loop('outbox_relay')`
produce the same top-level symptom — jobs accepted, nothing executing — and the
opposite evidence. A killed consumer leaves the backlog in Kafka, so consumer
lag climbs. A paused relay leaves the backlog in Postgres, so `outbox_events`
rows accumulate unpublished and age, while `worker-dispatcher` lag stays flat
because nothing is reaching Kafka to fall behind on. That contrast is the whole
reason the family is built first (plan 01 §7.1), and it is only a contrast if
**both** signals are asserted — a test that watched the outbox alone would pass
just as happily on a world where everything had stopped.

Why this tier. The relay tick is three transaction boundaries behind a Postgres
advisory-lock leader gate (ADR 0020), and "rows stayed unpublished and got
older" is a claim about `published_at`, `created_at` and a real clock. SQLite
has no advisory lock, so the unit tier has to inject leadership rather than take
it, and `NOW()` there is not the server's. Both halves of the assertion need the
real server.

What is stubbed, and why that does not weaken it: Kafka. The relay's publish
call is replaced by a recorder, so "published" means "the relay decided to
publish this row and marked it" — which is exactly the signal `get_outbox_status`
(WP-4.2) will read and exactly what the pause has to stop. The lag half is read
from the Redis-cached value the metrics loop maintains
(`kafka:consumer_lag:worker-dispatcher`), which is where every reader of lag on
this platform reads it from — the API's backpressure check and the agent's
`get_consumer_lag` both do. Standing up Redpanda to observe a number nobody
reads from the broker would test the harness.

Skipped automatically when Docker / testcontainers is unavailable, like every
other file in this tier.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.config import Settings
from app.models.base import Base
from app.models.outbox import OutboxEvent
from app.models.tenant import Tenant
from app.workers import dispatcher
from app.workers.control_loop_pause import ControlLoopName, pause_key_for
from sqlalchemy import func, select
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

#: Flat, and deliberately non-zero: a scenario's world has traffic in it, and
#: "lag stayed at the number it was" is the assertion. Zero would leave the test
#: unable to tell a flat reading from an absent one.
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

    A stub rather than a real Redis container: the pause check is one GET of one
    key, and the lag cache is one SET of one key. A second container would add a
    minute to the tier to prove that redis-py can round-trip a string.
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


async def _outbox_status(factory: Any) -> tuple[int, float | None]:
    """`unpublished_count` and `oldest_unpublished_age_s` — the two numbers
    WP-4.2's read tool will report, computed the same way here."""
    async with factory() as session:
        row = (
            await session.execute(
                select(
                    func.count(OutboxEvent.id),
                    func.min(OutboxEvent.created_at),
                ).where(OutboxEvent.published_at.is_(None))
            )
        ).one()
    count, oldest = int(row[0]), row[1]
    if oldest is None:
        return count, None
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=UTC)
    return count, (datetime.now(UTC) - oldest).total_seconds()


#: Shortened loop intervals for the window below, and how long the window is.
#: The real loops sleep 1 s (relay) and 60 s (metrics) between passes, which no
#: test should wait for. Patching the two module constants keeps the loop bodies
#: — and their own `asyncio.sleep` calls — completely untouched.
_FAST_INTERVAL = 0.02
_WINDOW_SECONDS = 0.4


async def _run_ticks(factory: Any, redis: _Redis, consumer: Any) -> list[bool]:
    """Let the real loops run for a short window, then cancel them.

    Deliberately NOT a patched `asyncio.sleep`: the relay holds a live asyncpg
    connection through its tick, and replacing the event loop's sleep for
    everything in that window would put a `CancelledError` wherever the driver
    happened to await next. Cancelling the task is how `worker_loop` stops these
    loops in production, so it is also the faithful way to stop them here.

    `CHAOS_ENABLED=true` is patched in for the whole window — that is gate 1, and
    without it the pause check never reaches Redis at all.
    """
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
    ), patch.object(dispatcher, "_METRICS_LOOP_INTERVAL", _FAST_INTERVAL):
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
    """A dispatcher consumer whose lag does not move.

    This is not a convenience — it is the world. The relay is not publishing, so
    nothing new arrives on `job.submitted`, so the group's lag is whatever it
    already was. A consumer that reported a climbing lag here would be modelling
    the *other* fault.
    """
    consumer = AsyncMock()
    consumer.consumer_lag = AsyncMock(return_value=_FLAT_LAG)
    consumer.in_flight = set()
    return consumer


async def test_a_paused_relay_grows_the_outbox_while_dispatcher_lag_stays_flat(
    session_factory: Any,
) -> None:
    """The contrast, asserted on both signals.

    Sequence: submit, pause, submit again, run several relay ticks and a couple
    of metrics passes. Afterwards every row must still be unpublished, the
    oldest must have aged, and the cached `worker-dispatcher` lag must be the
    flat value — not missing, not climbing.
    """
    tenant_id = await _make_tenant(session_factory)
    redis = _Redis()

    await _submit(session_factory, tenant_id, 4)
    before_count, before_age = await _outbox_status(session_factory)
    assert before_count == 4
    assert before_age is not None

    await redis.set(_PAUSE_KEY, "paused", ex=300)
    await asyncio.sleep(1.1)  # so "the oldest row aged" is measurable
    await _submit(session_factory, tenant_id, 3)

    entries = await _run_ticks(session_factory, redis, _flat_lag_consumer())

    after_count, after_age = await _outbox_status(session_factory)

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
    """TTL restores normal publishing with no manual step.

    The key is deleted rather than waited out — Redis expiry and an explicit
    delete are indistinguishable to the check, which is a GET, and waiting out a
    real TTL would put a minimum sleep in the tier for no extra coverage. What
    this proves is the half that matters: nothing calls a compensator, and the
    first tick that finds the key gone drains everything.
    """
    tenant_id = await _make_tenant(session_factory)
    redis = _Redis()

    await _submit(session_factory, tenant_id, 5)
    await redis.set(_PAUSE_KEY, "paused", ex=300)

    await _run_ticks(session_factory, redis, _flat_lag_consumer())
    assert (await _outbox_status(session_factory))[0] == 5

    await redis.delete(_PAUSE_KEY)  # what the TTL does, on its own clock

    await _run_ticks(session_factory, redis, _flat_lag_consumer())
    assert (await _outbox_status(session_factory))[0] == 0, (
        "the relay did not resume on its own after the flag went away"
    )


async def test_pausing_a_different_loop_leaves_the_relay_publishing(
    session_factory: Any,
) -> None:
    """`single_loop` is a blast-radius claim, so it gets an assertion.

    A key for one member must not stop another member's loop — the failure mode
    a shared key or a prefix match would produce, and one that would quietly
    widen every scenario's fault.
    """
    tenant_id = await _make_tenant(session_factory)
    redis = _Redis()

    await _submit(session_factory, tenant_id, 3)
    await redis.set(
        pause_key_for(ControlLoopName.STALE_RUNNING_SWEEP), "paused", ex=300
    )

    await _run_ticks(session_factory, redis, _flat_lag_consumer())

    assert (await _outbox_status(session_factory))[0] == 0, (
        "pausing another loop stopped the outbox relay"
    )

