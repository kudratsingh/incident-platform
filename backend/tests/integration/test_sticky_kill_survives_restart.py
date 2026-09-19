"""A fault that survives its own fix, on a real Redis (WO-R3-225, WP-10.0).

Everything here is the shipped code except the broker: a real `BaseKafkaConsumer` poll loop, the
real `_supervise_consumer` kill window, the real `restart_consumer_group` action and the real eval
reset sweep, against a real Redis. The mechanism is Redis state — `PXAT`, expiry and a `chaos:*`
SCAN — so an in-process fake would be testing the fake's arithmetic rather than Redis's.

Four things are proved together, because any three of them hold for the plain kill too: the
contrast (a non-sticky kill IS fixed by the restart action), that a sticky kill is not, that the
window is absolute however many restarts happen inside it, and that the reset ends it at once.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from app.config import Settings
from app.core.scopes import Scope
from app.dependencies import Principal
from app.mcp.registry import ToolContext
from app.mcp.tools.actions.restart_consumer_group import (
    RestartConsumerGroupInput,
    restart_consumer_group,
)
from app.workers import dispatcher, kafka_consumer
from app.workers.kafka_consumer import (
    BaseKafkaConsumer,
    kill_key_for,
    sticky_kill_key_for,
)
from redis.asyncio import Redis

try:
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    _HAS_TC = True
except Exception:  # pragma: no cover - testcontainers not installed
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
    reason="needs Docker + testcontainers",
)

_GROUP = "worker-dispatcher"
_KILL_KEY = kill_key_for(_GROUP)
_STICKY_KEY = sticky_kill_key_for(_GROUP)

#: Short enough that "recovers at the original expiry" is a few seconds rather than five minutes,
#: long enough to fit three restarts inside the window.
_TTL_SECONDS = 6

# `scripts/` is not a package on disk; make it importable, the way the unit tier already does.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_SCRIPTS = os.path.join(_ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


@pytest_asyncio.fixture(scope="module")
async def redis_url() -> AsyncGenerator[str, None]:
    """A real Redis. The whole point of this file is real expiry semantics."""
    container = DockerContainer("redis:7-alpine").with_exposed_ports(6379)
    container.start()
    try:
        wait_for_logs(container, "Ready to accept connections", timeout=60)
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"
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


class _StubKafka:
    """Just enough aiokafka for `BaseKafkaConsumer.run()` to poll and commit nothing."""

    def __init__(self, *_a: Any, **_kw: Any) -> None:
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def getmany(self, **_kw: Any) -> dict[Any, list[Any]]:
        await asyncio.sleep(0.02)
        return {}


class _CountingConsumer(BaseKafkaConsumer):
    """The real base class, with the broker stubbed and every start counted."""

    def __init__(self) -> None:
        super().__init__(topics=["job.submitted"], group_id=_GROUP)
        self.starts = 0

    async def start(self) -> None:
        await super().start()
        self.starts += 1

    async def handle_message(
        self,
        topic: str,
        key: str | None,
        value: dict[str, Any],
        *,
        partition: int = 0,
        offset: int = 0,
    ) -> None:  # pragma: no cover - no messages are delivered here
        return None


def _ctx(redis: Redis) -> ToolContext:
    return ToolContext(
        db=None,  # type: ignore[arg-type]
        redis=redis,
        principal=Principal(
            kind="service_account",
            tenant_id=uuid.uuid4(),
            scopes=frozenset({Scope.ACTIONS_EXECUTE.value}),
        ),
    )


async def _restart_action(redis: Redis, attempt: int) -> Any:
    """The Tier-1 action, with a FRESH idempotency key per attempt.

    A reused key returns a perfect success that did nothing, which is precisely the confusion this
    world would otherwise manufacture (LESSONS 2026-09-07)."""
    return await restart_consumer_group(
        RestartConsumerGroupInput(
            consumer_group=_GROUP, idempotency_key=f"sticky-{attempt}-{uuid.uuid4().hex}"
        ),
        _ctx(redis),
    )


async def _arm(redis: Redis, *, sticky: bool) -> float:
    """Set the kill the way the hook does, and return the deadline it stored."""
    deadline = time.time() + _TTL_SECONDS
    await redis.set(_KILL_KEY, kafka_consumer.KILL_FLAG_VALUE, ex=_TTL_SECONDS)
    if sticky:
        await redis.set(_STICKY_KEY, f"{deadline:.3f}", ex=_TTL_SECONDS)
    return deadline


async def _wait_for(predicate: Any, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


class _Supervised:
    """A supervised consumer, started and torn down around one test."""

    def __init__(self, redis: Redis) -> None:
        self.consumer = _CountingConsumer()
        self._patches = [
            patch(
                "app.workers.kafka_consumer.get_settings",
                return_value=Settings(chaos_enabled=True, environment="test"),
            ),
            patch(
                "app.workers.kafka_consumer.AIOKafkaConsumer",
                new=_StubKafka,
            ),
            patch("app.core.redis.get_redis_client", return_value=redis),
            patch.object(dispatcher, "_SUPERVISOR_POLL_SECONDS", 0.1),
        ]
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> _CountingConsumer:
        for p in self._patches:
            p.start()
        self.task = asyncio.create_task(
            dispatcher._supervise_consumer(self.consumer)
        )
        # The boot start() is the supervisor's, so wait for the consumer to be up before arming.
        await _wait_for(lambda: self.consumer.starts >= 1, 10.0, "the boot start")
        return self.consumer

    async def __aexit__(self, *_exc: Any) -> None:
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        await self.consumer.stop()
        for p in reversed(self._patches):
            p.stop()


def _reset_module() -> Any:
    """`scripts/reset_eval_state.py` — the real sweep, not a copy of its patterns."""
    return importlib.import_module("reset_eval_state")


# The contrast: without the flag, nothing changes


async def test_a_plain_kill_is_fixed_by_the_restart_action(redis: Redis) -> None:
    """Red-before, kept as the contrast: this is the world every scenario shipped before WP-10.0
    lives in, and the restart action still ends it in one call."""
    async with _Supervised(redis) as consumer:
        await _arm(redis, sticky=False)
        await _wait_for(
            lambda: consumer.chaos_killed, 10.0, "the consumer to see the kill"
        )
        starts_before = consumer.starts

        out = await _restart_action(redis, 1)
        assert out.kill_key_cleared is True

        await _wait_for(
            lambda: consumer.starts > starts_before, 10.0, "the consumer to come back"
        )
        assert await redis.get(_KILL_KEY) is None


# The packet


async def test_a_sticky_kill_survives_the_restart_action(redis: Redis) -> None:
    """The fault outlives its fix: the action clears the flag and says so, and the group is still
    down because the flag is back before the supervisor looks."""
    async with _Supervised(redis) as consumer:
        await _arm(redis, sticky=True)
        await _wait_for(
            lambda: consumer.chaos_killed, 10.0, "the consumer to see the kill"
        )
        starts_before = consumer.starts

        out = await _restart_action(redis, 1)
        assert out.kill_key_cleared is True, "the flag WAS there and WAS cleared"
        assert out.accepted is True

        # Several supervisor polls' worth of the window, and the consumer must not return.
        await asyncio.sleep(1.0)
        assert consumer.starts == starts_before, (
            "the sticky kill did not survive restart_consumer_group"
        )
        assert await redis.get(_KILL_KEY) == kafka_consumer.KILL_FLAG_VALUE, (
            "the flag was not re-armed"
        )
        assert await redis.get(_STICKY_KEY) is not None


async def test_the_restart_actions_reply_never_names_the_lab(redis: Redis) -> None:
    """ADR 0012 rule 1, on the response rather than the schema: the action must explain itself
    without a chaos key or a chaos reason, which is the leak it closed in v0.4.9."""
    await _arm(redis, sticky=True)
    out = await _restart_action(redis, 1)
    assert "chaos" not in json.dumps(out.model_dump(mode="json")).lower()
    # ...and it left the lab's own key alone.
    assert await redis.get(_STICKY_KEY) is not None


async def test_the_window_is_absolute_however_many_restarts_happen(
    redis: Redis,
) -> None:
    """The property the order names. Three restarts spread across the window, and recovery still
    lands at the original deadline — not `_TTL_SECONDS` after the last one."""
    async with _Supervised(redis) as consumer:
        deadline = await _arm(redis, sticky=True)
        await _wait_for(
            lambda: consumer.chaos_killed, 10.0, "the consumer to see the kill"
        )
        starts_before = consumer.starts

        for attempt in range(1, 4):
            await _restart_action(redis, attempt)
            await asyncio.sleep(0.4)
            assert consumer.starts == starts_before, (
                f"the consumer came back after restart {attempt}"
            )
            pttl = await redis.pttl(_KILL_KEY)
            # Redis's own answer: the re-armed flag expires at the original deadline, so its
            # remaining life shrinks across attempts and never resets to the full TTL.
            assert 0 < pttl <= (deadline - time.time() + 1) * 1000

        await _wait_for(
            lambda: consumer.starts > starts_before,
            (deadline - time.time()) + 8.0,
            "the consumer to recover at the original expiry",
        )
        # Recovery is on the far side of the deadline, and nowhere near a rolling window's.
        assert time.time() >= deadline - 0.5
        assert time.time() < deadline + 6.0
        assert await redis.get(_STICKY_KEY) is None, "the marker outlived its own TTL"


async def test_the_eval_reset_sweep_ends_it_at_once(redis: Redis) -> None:
    """`make eval-reset` clears both keys in one `chaos:*` scan, and the consumer comes straight
    back — the teardown a scenario depends on when it does not want to wait out the window."""
    reset = _reset_module()
    async with _Supervised(redis) as consumer:
        await _arm(redis, sticky=True)
        await _wait_for(
            lambda: consumer.chaos_killed, 10.0, "the consumer to see the kill"
        )
        await _restart_action(redis, 1)
        await asyncio.sleep(0.4)
        starts_before = consumer.starts

        cleared = await reset._clear_chaos_keys(redis)
        assert cleared >= 2, "the sweep did not reach both keys"
        assert await redis.get(_KILL_KEY) is None
        assert await redis.get(_STICKY_KEY) is None

        await _wait_for(
            lambda: consumer.starts > starts_before,
            10.0,
            "the consumer to recover after the reset",
        )
