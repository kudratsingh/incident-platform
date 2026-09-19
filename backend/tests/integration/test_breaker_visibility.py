"""The H7 proof: a breaker one process opened is visible to a reader in another.

The registry is a module-level dict and the MCP server is a separate process from the same
image (ADR 0006), so before ADR 0030 a read tool walked its own empty registry and reported
every breaker closed. This is the only test that proves otherwise: a real Redis, two
independent clients, and a reader whose registry is empty by construction. Red before the
change — the tool did not exist, and a registry-walking one would answer "closed".
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from app.core.breaker_state import (
    FAILURE_CLASS_CONNECTION,
    FAILURE_CLASS_TIMEOUT,
    breaker_key_for,
)
from app.core.circuit_breaker import CircuitBreaker
from app.core.scopes import Scope
from app.dependencies import Principal
from app.mcp.registry import ToolContext
from app.mcp.tools.circuit_breakers import (
    GetCircuitBreakersInput,
    GetCircuitBreakersOutput,
    get_circuit_breakers,
)
from redis.asyncio import Redis

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


def _find_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


@pytest_asyncio.fixture(scope="module")
async def redis_url() -> AsyncGenerator[str, None]:
    """A real Redis, so the two clients below are genuinely two clients."""
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
    """The process that owns the breaker."""
    client: Redis = Redis.from_url(redis_url, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def reader(redis_url: str) -> AsyncGenerator[Redis, None]:
    """The read surface: its own client, its own pool, no shared Python state."""
    client: Redis = Redis.from_url(redis_url, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


def _ctx(redis: Redis) -> ToolContext:
    return ToolContext(
        db=object(),  # type: ignore[arg-type]  — this tool never touches the database
        redis=redis,
        principal=Principal(
            kind="service_account",
            tenant_id=uuid.uuid4(),
            scopes=frozenset({Scope.TELEMETRY_READ.value}),
        ),
    )


async def _read(redis: Redis) -> GetCircuitBreakersOutput:
    return await get_circuit_breakers(GetCircuitBreakersInput(), _ctx(redis))


async def test_a_breaker_opened_in_one_process_reads_as_open_in_another(
    writer: Redis, reader: Redis
) -> None:
    """The whole point: the reader shares no Python state with the writer."""
    breaker = CircuitBreaker("bulk-api-sync", failure_threshold=3, recovery_timeout=30.0)

    async def _boom() -> None:
        raise ConnectionError("upstream refused")

    for _ in range(3):
        with pytest.raises(ConnectionError):
            await breaker.call(_boom)
        await breaker._record(changed=True, redis=writer)

    assert await writer.get(breaker_key_for("bulk-api-sync")) is not None

    out = await _read(reader)

    assert out.unknown_reason is None
    assert out.total == 1
    reading = out.breakers[0]
    assert reading.name == "bulk-api-sync"
    assert reading.state == "open"
    assert reading.failure_count == 3
    assert reading.failure_threshold == 3
    assert reading.last_failure_reason_class == FAILURE_CLASS_CONNECTION
    assert reading.last_state_change_at is not None
    assert reading.seconds_since_state_change is not None
    assert reading.reported_age_s >= 0.0


async def test_a_registered_breaker_reads_as_closed_before_it_ever_fails(
    writer: Redis, reader: Redis
) -> None:
    """A healthy breaker has to be sayable, or the reading only works when broken. The
    worker records its registry at boot for exactly this."""
    breaker = CircuitBreaker("quiet-dependency", failure_threshold=5, recovery_timeout=30.0)
    await breaker._record(changed=True, redis=writer)

    out = await _read(reader)

    reading = next(b for b in out.breakers if b.name == "quiet-dependency")
    assert reading.state == "closed"
    assert reading.failure_count == 0
    assert reading.last_failure_reason_class is None
    assert reading.last_state_change_at is None


async def test_the_failure_class_travels_and_the_message_does_not(
    writer: Redis, reader: Redis
) -> None:
    """A message can name a job, a URL or whatever injected the fault (ADR 0012), so only
    the class crosses the wire."""
    breaker = CircuitBreaker("classified", failure_threshold=1, recovery_timeout=30.0)

    async def _boom() -> None:
        raise TimeoutError("job 7f3a to https://api.internal/x timed out")

    with pytest.raises(TimeoutError):
        await breaker.call(_boom)
    await breaker._record(changed=True, redis=writer)

    raw = await writer.get(breaker_key_for("classified"))
    assert raw is not None
    assert "api.internal" not in raw
    assert "7f3a" not in raw

    reading = next(b for b in (await _read(reader)).breakers if b.name == "classified")
    assert reading.last_failure_reason_class == FAILURE_CLASS_TIMEOUT


async def test_an_unreachable_store_reads_as_unknown_not_as_all_closed(
    writer: Redis, redis_url: str
) -> None:
    """The failure that matters most: a reader that cannot reach the store must not
    report a healthy platform."""
    breaker = CircuitBreaker("bulk-api-sync", failure_threshold=1, recovery_timeout=30.0)

    async def _boom() -> None:
        raise ConnectionError("upstream refused")

    with pytest.raises(ConnectionError):
        await breaker.call(_boom)
    await breaker._record(changed=True, redis=writer)

    # A client pointed at a port nothing listens on, with no retries.
    dead: Redis = Redis.from_url(
        f"redis://localhost:{_find_free_port()}/0",
        decode_responses=True,
        socket_connect_timeout=0.5,
    )
    try:
        out = await _read(dead)
    finally:
        await dead.aclose()

    assert out.breakers == ()
    assert out.total == 0
    assert out.unknown_reason is not None


async def test_the_record_expires_rather_than_going_stale_forever(
    writer: Redis, reader: Redis
) -> None:
    """A TTL is what makes an absent breaker absent instead of a permanently closed one."""
    breaker = CircuitBreaker("expiring", failure_threshold=1, recovery_timeout=30.0)
    await breaker._record(changed=True, redis=writer)

    ttl = await writer.ttl(breaker_key_for("expiring"))
    assert ttl > 0

    await writer.delete(breaker_key_for("expiring"))  # what the TTL does, on its own clock

    out = await _read(reader)

    assert "expiring" not in {b.name for b in out.breakers}


async def test_the_key_is_not_swept_by_the_lab_namespace(writer: Redis) -> None:
    """`reset_eval_state` scans `chaos:*`. This key is a platform key, so it survives —
    stated here on a real Redis rather than only asserted on the constant."""
    breaker = CircuitBreaker("survivor", failure_threshold=1, recovery_timeout=30.0)
    await breaker._record(changed=True, redis=writer)

    chaos_keys = [k async for k in writer.scan_iter(match="chaos:*")]
    assert breaker_key_for("survivor") not in chaos_keys
    assert await writer.get(breaker_key_for("survivor")) is not None
