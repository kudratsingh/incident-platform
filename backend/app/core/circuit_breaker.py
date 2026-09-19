import asyncio
import enum
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from app.core.breaker_state import (
    BREAKER_REFRESH_INTERVAL_SECONDS,
    classify_failure,
    publish_breaker_state,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Circuit '{name}' is open — calls are being rejected")


class CircuitBreaker:
    """
    Three-state circuit breaker for async callables: CLOSED / OPEN / HALF_OPEN.

    HALF_OPEN admits exactly one probe; concurrent arrivals get CircuitOpenError,
    so a recovering upstream sees one request rather than the whole backlog. Every state
    change is also recorded outside the process, because the reader is in another one
    (ADR 0030).
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False
        self._lock = asyncio.Lock()

        # Wall clock beside the monotonic `_opened_at`, not instead of it: a duration
        # belongs on a clock nothing can step, and another process cannot render one.
        self._last_state_change_at: datetime | None = None
        self._last_failure_at: datetime | None = None
        self._last_failure_reason_class: str | None = None
        self._recorded_at_monotonic: float | None = None

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def last_state_change_at(self) -> datetime | None:
        """When the state last changed. `None` while it has never changed."""
        return self._last_state_change_at

    @property
    def last_failure_at(self) -> datetime | None:
        return self._last_failure_at

    @property
    def last_failure_reason_class(self) -> str | None:
        """The class of the last failure — never its message (ADR 0012)."""
        return self._last_failure_reason_class

    def _set_state(self, state: CircuitState) -> None:
        """Move to `state`, stamping the wall clock only on a real change."""
        if state is self._state:
            return
        self._state = state
        self._last_state_change_at = datetime.now(UTC)

    async def _record(self, *, changed: bool, redis: Any | None = None) -> None:
        """Record this breaker's state outside the process; throttled unless it changed."""
        now = time.monotonic()
        if (
            not changed
            and self._recorded_at_monotonic is not None
            and now - self._recorded_at_monotonic < BREAKER_REFRESH_INTERVAL_SECONDS
        ):
            return
        self._recorded_at_monotonic = now
        await publish_breaker_state(
            redis,
            name=self.name,
            state=self._state.value,
            failure_count=self._failure_count,
            failure_threshold=self.failure_threshold,
            recovery_timeout_s=self.recovery_timeout,
            last_state_change_at=self._last_state_change_at,
            last_failure_at=self._last_failure_at,
            last_failure_reason_class=self._last_failure_reason_class,
        )

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        # This caller owns the probe and must clear _probe_in_flight.
        is_probe = False

        async with self._lock:
            if self._state == CircuitState.OPEN:
                elapsed = time.monotonic() - (self._opened_at or 0)
                if elapsed >= self.recovery_timeout:
                    self._set_state(CircuitState.HALF_OPEN)
                    self._probe_in_flight = True
                    is_probe = True
                    logger.info("circuit half-open", extra={"circuit": self.name})
                else:
                    raise CircuitOpenError(self.name)
            elif self._state == CircuitState.HALF_OPEN:
                # A probe is in flight; without this every arrival would probe too.
                raise CircuitOpenError(self.name)

        # Outside the lock: a slow store must not hold a breaker others are waiting on.
        if is_probe:
            await self._record(changed=True)

        try:
            result = await fn()
        except CircuitOpenError:
            # A nested breaker rejected: our upstream was never reached, so this
            # is no probe outcome — undo the probe or HALF_OPEN strands forever.
            if is_probe:
                async with self._lock:
                    self._probe_in_flight = False
                    self._set_state(CircuitState.OPEN)
                    # _opened_at left alone: the next caller may retry at once.
                await self._record(changed=True)
            raise
        except Exception as exc:
            await self._on_failure(exc)
            raise
        else:
            await self._on_success()
            return result

    async def _on_success(self) -> None:
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                logger.info("circuit closed (probe succeeded)", extra={"circuit": self.name})
            was = self._state
            self._set_state(CircuitState.CLOSED)
            self._failure_count = 0
            self._opened_at = None
            self._probe_in_flight = False

        await self._record(changed=was is not CircuitState.CLOSED)

    async def _on_failure(self, exc: Exception) -> None:
        async with self._lock:
            self._probe_in_flight = False
            self._failure_count += 1
            self._last_failure_at = datetime.now(UTC)
            self._last_failure_reason_class = classify_failure(exc)
            was = self._state
            tripped = self._failure_count >= self.failure_threshold
            if self._state == CircuitState.HALF_OPEN or tripped:
                self._set_state(CircuitState.OPEN)
                self._opened_at = time.monotonic()
                logger.warning(
                    "circuit opened",
                    extra={
                        "circuit": self.name,
                        "failure_count": self._failure_count,
                        "error": str(exc),
                    },
                )

        # A failure below the threshold still moves the count, so it is worth recording —
        # throttled, unlike the transition.
        await self._record(changed=was is not self._state)


_registry: dict[str, CircuitBreaker] = {}


def get_circuit_breaker(
    name: str,
    failure_threshold: int = 5,
    recovery_timeout: float = 30.0,
) -> CircuitBreaker:
    if name not in _registry:
        _registry[name] = CircuitBreaker(
            name,
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
        )
    return _registry[name]


def registered_breakers() -> tuple[CircuitBreaker, ...]:
    """Every breaker this process has registered, in name order."""
    return tuple(sorted(_registry.values(), key=lambda b: b.name))


async def record_registered_breakers(redis: Any | None = None) -> None:
    """Record every breaker in this process, so a closed one is readable before it fails."""
    for breaker in registered_breakers():
        await breaker._record(changed=True, redis=redis)
