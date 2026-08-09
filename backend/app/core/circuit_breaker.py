import asyncio
import enum
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

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
    Three-state circuit breaker for async callables.

    CLOSED  — all calls go through; failures are counted
    OPEN    — calls fail immediately; reopens after recovery_timeout seconds
    HALF_OPEN — exactly one probe call is allowed through; success closes,
                failure reopens. Callers arriving while that probe is still in
                flight are rejected with CircuitOpenError, so a recovering
                upstream sees one request rather than the whole backlog.
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

    @property
    def state(self) -> CircuitState:
        return self._state

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        # Set under the lock when this caller is the one probing a recovering
        # upstream; it owns clearing _probe_in_flight on every exit path.
        is_probe = False

        async with self._lock:
            if self._state == CircuitState.OPEN:
                elapsed = time.monotonic() - (self._opened_at or 0)
                if elapsed >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._probe_in_flight = True
                    is_probe = True
                    logger.info("circuit half-open", extra={"circuit": self.name})
                else:
                    raise CircuitOpenError(self.name)
            elif self._state == CircuitState.HALF_OPEN:
                # A probe is already in flight. The lock is released before
                # `await fn()`, so without this branch every concurrent arrival
                # would see HALF_OPEN and probe too.
                raise CircuitOpenError(self.name)

        try:
            result = await fn()
        except CircuitOpenError:
            # A *nested* breaker rejected the call — this breaker's upstream was
            # never reached, so the rejection must not count as a probe outcome
            # and _on_failure/_on_success are deliberately bypassed. That means
            # this branch has to undo the probe itself; leaving _probe_in_flight
            # set would strand the breaker in HALF_OPEN, rejecting forever.
            if is_probe:
                async with self._lock:
                    self._probe_in_flight = False
                    self._state = CircuitState.OPEN
                    # _opened_at is left alone on purpose: the probe never
                    # reached the upstream, so the next caller may retry
                    # immediately rather than wait out another recovery_timeout.
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
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._opened_at = None
            self._probe_in_flight = False

    async def _on_failure(self, exc: Exception) -> None:
        async with self._lock:
            self._probe_in_flight = False
            self._failure_count += 1
            tripped = self._failure_count >= self.failure_threshold
            if self._state == CircuitState.HALF_OPEN or tripped:
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                logger.warning(
                    "circuit opened",
                    extra={
                        "circuit": self.name,
                        "failure_count": self._failure_count,
                        "error": str(exc),
                    },
                )


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
