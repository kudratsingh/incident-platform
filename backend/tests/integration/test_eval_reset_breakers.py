"""The reset really closes a breaker the lab opened, on a real Redis (WO-R3-311, ADR 0036).

End to end, in the order a packet hits it: `degrade_downstream` sets the flag, the shipped
`bulk-api-sync` breaker opens under it and publishes that, `make eval-reset`'s breaker step runs,
and a reader in another process — its own client, no shared Python state — reads every breaker
closed with the failure fields null. Then the half a deleted key cannot buy: the registry that
opened the breaker admits calls again without being restarted.

Red before ADR 0036: `_reset_breaker_states` did not exist, the reset left `breaker:state:*`
alone for its whole 24 h TTL, and deleting the key by hand only made the breaker *absent* from
the reading while the registry kept refusing calls and wrote the open state back.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from app.config import Settings
from app.core import breaker_state
from app.core.circuit_breaker import CircuitState
from app.core.scopes import Scope
from app.dependencies import Principal
from app.mcp.registry import ToolContext
from app.mcp.tools.chaos.degrade_downstream import (
    DegradeDownstreamInput,
    degrade_downstream,
)
from app.mcp.tools.circuit_breakers import (
    GetCircuitBreakersInput,
    GetCircuitBreakersOutput,
    get_circuit_breakers,
)
from app.workers import async_tasks
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

#: The breaker's threshold is 3, so one job's fan-out opens it.
_ENDPOINTS = 3


def _find_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


@pytest_asyncio.fixture(scope="module")
async def redis_url() -> AsyncGenerator[str, None]:
    """A real Redis: the point of this file is two clients that share nothing."""
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
async def worker(redis_url: str) -> AsyncGenerator[Redis, None]:
    """The process that owns the breaker and the flag."""
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


@pytest.fixture(autouse=True)
def fresh_breaker() -> Iterator[None]:
    """The registry is a process-wide dict, so each test starts the breaker closed and
    leaves it that way — including the epoch it has observed."""

    def _reset() -> None:
        breaker = async_tasks.bulk_api_breaker()
        breaker._state = CircuitState.CLOSED
        breaker._failure_count = 0
        breaker._opened_at = None
        breaker._probe_in_flight = False
        breaker._last_state_change_at = None
        breaker._last_failure_at = None
        breaker._last_failure_reason_class = None
        breaker._recorded_at_monotonic = None
        breaker._observed_reset_at = None

    _reset()
    yield
    _reset()


def _ctx(redis: Redis) -> ToolContext:
    return ToolContext(
        db=object(),  # type: ignore[arg-type]  — neither tool here touches the database
        redis=redis,
        principal=Principal(
            kind="service_account",
            tenant_id=uuid.uuid4(),
            scopes=frozenset({Scope.TELEMETRY_READ.value, Scope.CHAOS_INVOKE.value}),
        ),
    )


async def _publish(_pct: int, _msg: str) -> None:
    return None


async def _read(redis: Redis) -> GetCircuitBreakersOutput:
    return await get_circuit_breakers(GetCircuitBreakersInput(), _ctx(redis))


def _as_worker(client: Redis) -> Any:
    """Point the processor's flag read and the breaker's own publish at this Redis."""
    return patch.multiple(
        async_tasks,
        get_settings=lambda: Settings(chaos_enabled=True, environment="test"),
        get_redis_client=lambda: client,
    )


async def _open_the_breaker(worker: Redis) -> None:
    """The lab's own route in: the chaos tool sets the flag, the processor runs, the
    shipped breaker crosses its threshold."""
    await degrade_downstream(
        DegradeDownstreamInput(mode="fail", ttl_seconds=300), _ctx(worker)
    )
    with _as_worker(worker), patch.object(breaker_state, "_client", lambda: worker):
        with pytest.raises(RuntimeError):
            await async_tasks.process_bulk_api_sync(
                {"endpoint_count": _ENDPOINTS}, _publish
            )
    assert async_tasks.bulk_api_breaker().state is CircuitState.OPEN


async def test_the_reset_leaves_every_breaker_closed_with_the_failure_fields_null(
    worker: Redis, reader: Redis
) -> None:
    await _open_the_breaker(worker)

    opened = next(b for b in (await _read(reader)).breakers if b.name == "bulk-api-sync")
    assert opened.state == CircuitState.OPEN.value
    assert opened.failure_count >= 3

    with patch.object(breaker_state, "_client", lambda: worker):
        assert await reset_eval_state._reset_breaker_states(worker) == 1

    after = await _read(reader)
    assert after.unknown_reason is None
    assert after.total >= 1
    for reading in after.breakers:
        assert reading.state == CircuitState.CLOSED.value
        assert reading.failure_count == 0
        assert reading.last_failure_at is None
        assert reading.last_failure_reason_class is None
        assert reading.last_state_change_at is None
        assert reading.seconds_since_state_change is None
    # Still a reading, not an absence: a deleted key reads as unknown, and a world audit
    # cannot assert "closed" about a breaker that is missing (ADR 0030).
    assert "bulk-api-sync" in {b.name for b in after.breakers}


async def test_the_registry_admits_calls_again_without_a_restart(
    worker: Redis, reader: Redis
) -> None:
    """The in-process half. `make eval-reset` cannot restart the worker, so a breaker that
    is only closed in the record would keep refusing every `bulk_api_sync` job."""
    await _open_the_breaker(worker)
    breaker = async_tasks.bulk_api_breaker()

    with patch.object(breaker_state, "_client", lambda: worker):
        await reset_eval_state._clear_chaos_keys(worker)  # the fault goes first
        await reset_eval_state._reset_breaker_states(worker)
        assert await worker.get(async_tasks.downstream_flag_key()) is None

        # A *benign* degradation for the second run, so the organic path's 10%-per-endpoint
        # failure cannot decide this test: every call answers late and succeeds if the
        # breaker lets it through, and an open breaker fails the whole job instead.
        await degrade_downstream(
            DegradeDownstreamInput(mode="slow", delay_ms=1), _ctx(worker)
        )
        with _as_worker(worker):
            result = await async_tasks.process_bulk_api_sync(
                {"endpoint_count": _ENDPOINTS}, _publish
            )

    assert result["endpoints_synced"] == _ENDPOINTS
    assert breaker.state is CircuitState.CLOSED

    reading = next(b for b in (await _read(reader)).breakers if b.name == "bulk-api-sync")
    assert reading.state == CircuitState.CLOSED.value
    assert reading.failure_count == 0


async def test_the_registry_cannot_write_its_remembered_failure_back(
    worker: Redis, reader: Redis
) -> None:
    """The contamination this closes: the reset's clean record has to survive the next
    thing the owning process says about the breaker, whatever prompts it."""
    await _open_the_breaker(worker)
    breaker = async_tasks.bulk_api_breaker()

    with patch.object(breaker_state, "_client", lambda: worker):
        await reset_eval_state._reset_breaker_states(worker)
        await breaker._record(changed=True, redis=worker)

    reading = next(b for b in (await _read(reader)).breakers if b.name == "bulk-api-sync")
    assert reading.state == CircuitState.CLOSED.value
    assert reading.last_failure_reason_class is None
    assert breaker.state is CircuitState.CLOSED


async def test_a_breaker_that_opens_after_the_reset_is_still_reported(
    worker: Redis, reader: Redis
) -> None:
    """The negative control. The signal clears what happened before it; a fault the next
    scenario causes must still reach the reading, or the reset would be an off switch."""
    with patch.object(breaker_state, "_client", lambda: worker):
        await reset_eval_state._reset_breaker_states(worker)
    await _open_the_breaker(worker)

    reading = next(b for b in (await _read(reader)).breakers if b.name == "bulk-api-sync")
    assert reading.state == CircuitState.OPEN.value
    assert reading.failure_count >= 3
    assert reading.last_failure_reason_class is not None
