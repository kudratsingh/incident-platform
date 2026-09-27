"""The lag clock keeps ticking through a consumer restart, on a real broker (WO-R3-356, ADR 0040).

Shipped code throughout: `JobDispatcherConsumer` on Redpanda, `_supervise_consumer`, `_metrics_loop`
at the demo's 5-s clock, the `restart_consumer_group` action, and a real Redis for the lag keys.
The one injected fault is the one the eighth take recorded: an offset query on the consumer being
restarted that does not come back. The claim: after the restart, the ring gains a sample within two
ticks of the new consumer's start, and the lag value key is never absent for more than one tick.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from app.config import get_settings
from app.core.consumer_lag import parse_samples
from app.core.scopes import Scope
from app.dependencies import Principal
from app.mcp.registry import ToolContext
from app.mcp.tools.actions.restart_consumer_group import (
    RestartConsumerGroupInput,
    restart_consumer_group,
)
from app.workers import dispatcher
from app.workers.dispatcher import (
    BACKPRESSURE_LAG_KEY,
    LAG_SAMPLES_KEY,
    JobDispatcherConsumer,
)
from app.workers.kafka_consumer import KILL_FLAG_VALUE, kill_key_for
from redis.asyncio import Redis

try:
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    _HAS_TC = True
except Exception:  # pragma: no cover - testcontainers not installed
    _HAS_TC = False


def _has_docker() -> bool:
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=True)
        return True
    except Exception:  # pragma: no cover - environment-dependent
        return False


pytestmark = pytest.mark.skipif(
    not _HAS_TC or not _has_docker(), reason="needs Docker + testcontainers"
)

# The demo stack's clock; the claim is stated in its ticks.
_TICK_SECONDS = 5.0
_SAMPLE_WITHIN_SECONDS = 2 * _TICK_SECONDS


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


@pytest_asyncio.fixture(scope="module")
async def redpanda() -> AsyncGenerator[str, None]:
    """Redpanda on a port pinned before start, so the advertised address matches."""
    port = _free_port()
    container = (
        DockerContainer("redpandadata/redpanda:v24.1.7")
        .with_command(
            "redpanda start "
            "--smp 1 --memory 512M --reserve-memory 0M "
            "--overprovisioned --node-id 0 --check=false "
            f"--kafka-addr PLAINTEXT://0.0.0.0:{port} "
            f"--advertise-kafka-addr PLAINTEXT://localhost:{port}"
        )
        .with_bind_ports(port, port)
    )
    container.start()
    try:
        wait_for_logs(container, "Successfully started Redpanda!", timeout=60)
        yield f"localhost:{port}"
    finally:
        container.stop()


@pytest_asyncio.fixture(scope="module")
async def redis_url() -> AsyncGenerator[str, None]:
    container = DockerContainer("redis:7-alpine").with_exposed_ports(6379)
    container.start()
    try:
        wait_for_logs(container, "Ready to accept connections", timeout=60)
        host = container.get_container_host_ip()
        yield f"redis://{host}:{container.get_exposed_port(6379)}/0"
    finally:
        container.stop()


@pytest_asyncio.fixture
async def redis(redis_url: str) -> AsyncGenerator[Redis, None]:
    client = Redis.from_url(redis_url, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


class _Dispatcher(JobDispatcherConsumer):
    """The real consumer on its own topic and group, stamping each completed start()."""

    def __init__(self, topic: str, group: str) -> None:
        super().__init__(session_factory=None, redis=None)  # type: ignore[arg-type]
        self.topics = [topic]
        self.group_id = group
        self.started_at: list[float] = []

    async def start(self) -> None:
        await super().start()
        self.started_at.append(time.time())

    async def handle_message(self, *_a: Any, **_kw: Any) -> None:  # pragma: no cover
        return None


async def _wait_for(predicate: Any, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what}")


async def _sample_times(redis: Redis) -> list[datetime]:
    return [s.measured_at for s in parse_samples(await redis.get(LAG_SAMPLES_KEY))]


async def _watch_value_key(redis: Redis, absences: list[float]) -> None:
    """Record every stretch the value key was absent, in seconds, until cancelled."""
    gone_since: float | None = None
    try:
        while True:
            present = await redis.exists(BACKPRESSURE_LAG_KEY)
            now = time.monotonic()
            if not present and gone_since is None:
                gone_since = now
            elif present and gone_since is not None:
                absences.append(now - gone_since)
                gone_since = None
            await asyncio.sleep(0.1)
    finally:
        if gone_since is not None:
            absences.append(time.monotonic() - gone_since)


def _restart_action(redis: Redis, group: str) -> Any:
    ctx = ToolContext(
        db=None,  # type: ignore[arg-type]
        redis=redis,
        principal=Principal(
            kind="service_account",
            tenant_id=uuid.uuid4(),
            scopes=frozenset({Scope.ACTIONS_EXECUTE.value}),
        ),
    )
    inp = RestartConsumerGroupInput(
        consumer_group=group, idempotency_key=f"lag-clock-{uuid.uuid4().hex}"
    )
    return restart_consumer_group(inp, ctx)


async def _kill_and_restart(
    redpanda: str, redis: Redis, monkeypatch: pytest.MonkeyPatch, *, hang_old_client: bool
) -> tuple[float, list[datetime], list[float]]:
    """Run one kill -> restart; return the new start time, the ring's times, the absences."""
    # 1. Point the shipped code at these containers, at the demo's clock, with the lab on.
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", redpanda)
    monkeypatch.setenv("CHAOS_ENABLED", "true")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("METRICS_LOOP_INTERVAL_SECONDS", str(_TICK_SECONDS))
    get_settings.cache_clear()

    topic = f"lag-clock.{uuid.uuid4().hex[:8]}"
    group = f"lag-clock-{uuid.uuid4().hex[:8]}"
    admin = AIOKafkaAdminClient(bootstrap_servers=redpanda)
    await admin.start()
    try:
        await admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)])
    finally:
        await admin.close()

    consumer = _Dispatcher(topic, group)
    absences: list[float] = []
    release_hang = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    patches = [
        patch("app.core.redis.get_redis_client", return_value=redis),
        patch.object(dispatcher, "_SUPERVISOR_POLL_SECONDS", 0.1),
        # The alert rules need Postgres and have their own integration test.
        patch.object(dispatcher.alert_rules, "evaluate_alert_rules", AsyncMock(return_value=None)),
    ]
    for p in patches:
        p.start()
    try:
        # 2. Supervisor and metrics loop running; wait for the first sample.
        tasks.append(asyncio.create_task(dispatcher._supervise_consumer(consumer)))
        tasks.append(asyncio.create_task(dispatcher._metrics_loop(redis, consumer, None)))  # type: ignore[arg-type]

        async def _has_sample() -> bool:
            return bool(await _sample_times(redis))

        await _wait_for(_has_sample, 4 * _TICK_SECONDS, "the first lag sample")
        tasks.append(asyncio.create_task(_watch_value_key(redis, absences)))

        # 3. Kill the group the way the lab does, and wait for the consumer to see it.
        await redis.set(kill_key_for(group), KILL_FLAG_VALUE, ex=300)

        async def _killed() -> bool:
            return consumer.chaos_killed

        await _wait_for(_killed, 10.0, "the consumer to see the kill")

        # 4. Take 8's shape: the next pass's offset query on the OLD client never answers.
        if hang_old_client:
            old = consumer._consumer
            assert old is not None
            real_committed = old.committed
            hang_started = asyncio.Event()

            async def _hung(tp: Any) -> Any:
                hang_started.set()
                await release_hang.wait()
                return await real_committed(tp)

            old.committed = _hung

            async def _in_flight() -> bool:
                return hang_started.is_set()

            await _wait_for(_in_flight, 2 * _TICK_SECONDS, "a pass to query the old client")

        # 5. The remediation: clear the kill; the supervisor stops and starts the consumer.
        out = await _restart_action(redis, group)
        assert out.kill_key_cleared is True

        async def _restarted() -> bool:
            return len(consumer.started_at) >= 2

        await _wait_for(_restarted, 30.0, "the supervisor to start the consumer again")
        started = consumer.started_at[-1]

        # 6. Watch past the target, so a sample that lands late is measured rather than missed.
        async def _sampled_after_start() -> bool:
            return any(t.timestamp() > started for t in await _sample_times(redis))

        try:
            await _wait_for(
                _sampled_after_start,
                (started + _SAMPLE_WITHIN_SECONDS + _TICK_SECONDS) - time.time(),
                "a sample after the restart",
            )
        except AssertionError:
            pass
        return started, await _sample_times(redis), absences
    finally:
        release_hang.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await consumer.stop()
        for p in reversed(patches):
            p.stop()
        get_settings.cache_clear()


def _assert_the_clock_kept_ticking(
    started: float, samples: list[datetime], absences: list[float]
) -> None:
    after = sorted(t.timestamp() for t in samples if t.timestamp() > started)
    assert after, "no lag sample was written after the consumer restarted"
    first = after[0] - started
    assert first <= _SAMPLE_WITHIN_SECONDS, (
        f"first sample {first:.1f} s after the restart; the target is two ticks "
        f"({_SAMPLE_WITHIN_SECONDS:.0f} s)"
    )
    longest = max(absences, default=0.0)
    assert longest <= _TICK_SECONDS, (
        f"the lag value was absent for {longest:.1f} s, more than one tick"
    )


async def test_a_hung_query_on_the_restarted_client_costs_one_tick(
    redpanda: str, redis: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The eighth take: without the bound, this pass waited 40 s and no sample landed for 45."""
    started, samples, absences = await _kill_and_restart(
        redpanda, redis, monkeypatch, hang_old_client=True
    )
    _assert_the_clock_kept_ticking(started, samples, absences)


async def test_a_plain_kill_and_restart_keeps_the_clock(
    redpanda: str, redis: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The contrast with no injected fault: a restart alone never costs the reading."""
    started, samples, absences = await _kill_and_restart(
        redpanda, redis, monkeypatch, hang_old_client=False
    )
    _assert_the_clock_kept_ticking(started, samples, absences)
