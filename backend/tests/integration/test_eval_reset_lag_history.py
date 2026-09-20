"""The consumer-lag history survives a reset, on a real Redis (WO-R3-333, ADR 0038).

The demo's third live take opened its fifteen-minute lag chart on two points while the fault
it exists to show was climbing 0 → 10 → 30: `make eval-reset` deleted the recorded window on
its way through, so the console started from cold every time. The window is history, and its
TTL is deliberately longer than the value key's (ADR 0037) precisely so it outlives the pass
that wrote it — deleting it on the reset boundary contradicted the reason it is kept.

Why this tier rather than a mock. The claim is about Redis semantics that a fake does not
have: that the reset's `SCAN`-and-delete patterns do not match the window key, that the window
keeps its own long TTL while the value key keeps its short one, and that a reader in another
process — its own client, no shared Python state — still sees every sample afterwards.

Red before the change: `reset_eval_state._clear_lag_samples` deleted
`kafka:consumer_lag:worker-dispatcher:samples` on every reset, so the first test here failed
with an empty window and the second had nothing left to read.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from app.core.consumer_lag import (
    LAG_SAMPLES_KEEP,
    LAG_SAMPLES_TTL,
    LAG_SAMPLES_WINDOW_SECONDS,
    LIVE_REFRESHED_GROUP,
    lag_key,
    read_lag,
    record_lag_sample,
    samples_key,
)
from app.utils.backpressure import BACKPRESSURE_LAG_KEY
from redis.asyncio import Redis

REPO_ROOT = Path(__file__).resolve().parents[3]

# `scripts/` isn't a package on disk; make the reset importable the way it is in the image.
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import reset_eval_state  # noqa: E402  # type: ignore[import-not-found]

try:
    import docker  # noqa: F401
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

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
    reason="needs Docker + testcontainers",
)

#: The value key's own TTL, as `app/workers/dispatcher.py` writes it. Short on purpose:
#: `check_backpressure` gates submissions on the number, so it must be fresh or absent.
_VALUE_TTL_SECONDS = 90


def _find_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


@pytest_asyncio.fixture(scope="module")
async def redis_url() -> AsyncGenerator[str, None]:
    host_port = _find_free_port()
    container = (
        DockerContainer("redis:7-alpine")
        .with_command(f"redis-server --port {host_port}")
        .with_bind_ports(host_port, host_port)
    )
    container.start()
    try:
        wait_for_logs(container, "Ready to accept connections", timeout=60)
        yield f"redis://localhost:{host_port}/0"
    finally:
        container.stop()


@pytest_asyncio.fixture
async def writer(redis_url: str) -> AsyncGenerator[Redis, None]:
    """The metrics loop's client: it writes the value and the window."""
    client: Redis = Redis.from_url(redis_url, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def reader(redis_url: str) -> AsyncGenerator[Redis, None]:
    """The read surface: its own client, its own pool."""
    client: Redis = Redis.from_url(redis_url, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


async def _a_take_that_ran(writer: Redis) -> list[int]:
    """One take's worth of measurements, plus the residue a reset is there to clear."""
    lags = [0, 0, 10, 30, 30]
    for lag in lags:
        await record_lag_sample(writer, lag, group=LIVE_REFRESHED_GROUP)
    await writer.set(BACKPRESSURE_LAG_KEY, str(lags[-1]), ex=_VALUE_TTL_SECONDS)

    # The world the reset is actually for: a kill flag, a poisoned read cache, a pause.
    await writer.set("chaos:kill:worker-dispatcher", "1")
    await writer.set("chaos:latency:worker-dispatcher", "500")
    await writer.set("cache:job:abc:def", "{}")
    await writer.set("dag:paused:abc", "by-the-agent")
    return list(reversed(lags))


async def _run_the_resets_redis_steps(redis: Redis) -> None:
    """Every step of `reset()` that touches Redis, in the order it runs them. The full
    `reset()` needs Postgres and the seeder; this is the half the window lives in."""
    await reset_eval_state._clear_chaos_keys(redis)
    await reset_eval_state._clear_job_read_cache(redis)
    await reset_eval_state._clear_scheduled_replays(redis)
    await reset_eval_state._clear_dag_pauses(redis)
    # The step WO-R3-333 removed, called here when the tree still has one. Without this
    # the test would go green on the version it is meant to be red on, by simply not
    # calling the function that broke the chart.
    clear_window = getattr(reset_eval_state, "_clear_lag_samples", None)
    if clear_window is not None:  # pragma: no cover - absent since WO-R3-333
        await clear_window(redis)
    await reset_eval_state._reseed_hot_set(redis)
    await reset_eval_state._reset_breaker_states(redis)


async def test_the_recorded_lag_window_survives_the_resets_redis_sweep(
    writer: Redis, reader: Redis
) -> None:
    newest_first = await _a_take_that_ran(writer)

    before = await read_lag(reader, LIVE_REFRESHED_GROUP)
    assert [s.lag for s in before.recent_samples] == newest_first

    await _run_the_resets_redis_steps(writer)

    # The residue is gone — the sweep really ran.
    assert await reader.exists("chaos:kill:worker-dispatcher") == 0
    assert await reader.exists("cache:job:abc:def") == 0
    assert await reader.exists("dag:paused:abc") == 0

    after = await read_lag(reader, LIVE_REFRESHED_GROUP)
    assert [s.lag for s in after.recent_samples] == newest_first, (
        "the reset cleared the lag window — the demo chart opens on two points while "
        "the fault it is drawn to show is climbing"
    )
    assert [s.measured_at for s in after.recent_samples] == [
        s.measured_at for s in before.recent_samples
    ]
    assert after.lag == 30

    # And the window keeps its own long TTL, so it outlives the pass that wrote it —
    # which is the property the deletion was quietly cancelling.
    window_ttl = await reader.ttl(samples_key(LIVE_REFRESHED_GROUP))
    assert LAG_SAMPLES_WINDOW_SECONDS < window_ttl <= LAG_SAMPLES_TTL
    value_ttl = await reader.ttl(lag_key(LIVE_REFRESHED_GROUP))
    assert 0 < value_ttl <= _VALUE_TTL_SECONDS


async def test_the_window_outlives_the_value_it_was_measured_beside(
    writer: Redis, reader: Redis
) -> None:
    """The asymmetry, end to end on a real Redis. The value must be fresh-or-absent, so it
    expires; history is most wanted at the moment the pass that writes it stopped, so the
    window is still there with every sample dated. Nothing is invented to fill the gap."""
    newest_first = await _a_take_that_ran(writer)
    await _run_the_resets_redis_steps(writer)

    # The metrics loop stopped: the value's 90 s TTL runs out, exactly as it would.
    assert await writer.delete(lag_key(LIVE_REFRESHED_GROUP)) == 1

    reading = await read_lag(reader, LIVE_REFRESHED_GROUP)
    assert reading.lag is None
    assert reading.lag_known is False
    assert reading.measured_at is None, "an undated window must not date an absent value"
    assert [s.lag for s in reading.recent_samples] == newest_first
    assert len(reading.recent_samples) <= LAG_SAMPLES_KEEP
