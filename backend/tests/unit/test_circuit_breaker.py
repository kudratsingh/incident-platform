import asyncio

import pytest
from app.core.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
)


async def _ok() -> str:
    return "ok"


async def _fail() -> str:
    raise RuntimeError("boom")


@pytest.fixture
def breaker() -> CircuitBreaker:
    return CircuitBreaker("test", failure_threshold=3, recovery_timeout=30.0)


async def test_starts_closed(breaker: CircuitBreaker) -> None:
    assert breaker.state == CircuitState.CLOSED


async def test_success_stays_closed(breaker: CircuitBreaker) -> None:
    await breaker.call(_ok)
    assert breaker.state == CircuitState.CLOSED


async def test_failures_below_threshold_stay_closed(breaker: CircuitBreaker) -> None:
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)
    assert breaker.state == CircuitState.CLOSED


async def test_threshold_opens_circuit(breaker: CircuitBreaker) -> None:
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)
    assert breaker.state == CircuitState.OPEN


async def test_open_rejects_immediately(breaker: CircuitBreaker) -> None:
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)

    with pytest.raises(CircuitOpenError):
        await breaker.call(_ok)


async def test_success_resets_failure_count(breaker: CircuitBreaker) -> None:
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)
    await breaker.call(_ok)
    # failure count reset — need 3 more failures to open
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)
    assert breaker.state == CircuitState.CLOSED


async def test_half_open_after_timeout(breaker: CircuitBreaker) -> None:
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)

    # Manually wind back the clock so the timeout looks elapsed
    breaker._opened_at = breaker._opened_at - 31  # type: ignore[operator]

    # Next call should transition to HALF_OPEN and execute
    result = await breaker.call(_ok)
    assert result == "ok"
    assert breaker.state == CircuitState.CLOSED


async def test_half_open_failure_reopens(breaker: CircuitBreaker) -> None:
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)

    breaker._opened_at = breaker._opened_at - 31  # type: ignore[operator]

    with pytest.raises(RuntimeError):
        await breaker.call(_fail)

    assert breaker.state == CircuitState.OPEN


# --- HALF_OPEN admits exactly one probe (concurrent callers) ---------------


async def _open_and_expire(breaker: CircuitBreaker) -> None:
    """Trip the breaker, then wind the clock back past recovery_timeout."""
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)
    assert breaker.state == CircuitState.OPEN
    breaker._opened_at = breaker._opened_at - (breaker.recovery_timeout + 1)  # type: ignore[operator]


async def test_half_open_admits_only_one_probe(breaker: CircuitBreaker) -> None:
    await _open_and_expire(breaker)

    gate = asyncio.Event()
    entered = asyncio.Event()
    calls = 0

    async def _gated() -> str:
        nonlocal calls
        calls += 1
        entered.set()
        await gate.wait()
        return "ok"

    probe = asyncio.create_task(breaker.call(_gated))
    await entered.wait()  # task A is inside fn(), breaker is HALF_OPEN

    # Task B arrives while the probe is still in flight: it must be rejected,
    # and it must NOT have executed fn(). Driven as a task (rather than awaited
    # inline) so the unfixed behaviour — B falling through into the gated fn —
    # fails this test instead of hanging it on the same gate.
    concurrent = asyncio.create_task(breaker.call(_gated))
    await asyncio.sleep(0.05)
    assert calls == 1, "HALF_OPEN admitted a second concurrent probe"
    assert concurrent.done()
    with pytest.raises(CircuitOpenError):
        await concurrent

    gate.set()
    assert await probe == "ok"
    assert breaker.state == CircuitState.CLOSED

    # The breaker is usable again once the probe resolved.
    assert await breaker.call(_gated) == "ok"
    assert calls == 2


async def test_half_open_probe_failure_reopens_and_frees_the_probe_slot(
    breaker: CircuitBreaker,
) -> None:
    await _open_and_expire(breaker)

    gate = asyncio.Event()
    entered = asyncio.Event()
    calls = 0

    async def _gated_fail() -> str:
        nonlocal calls
        calls += 1
        entered.set()
        await gate.wait()
        raise RuntimeError("boom")

    probe = asyncio.create_task(breaker.call(_gated_fail))
    await entered.wait()

    concurrent = asyncio.create_task(breaker.call(_gated_fail))
    await asyncio.sleep(0.05)
    assert calls == 1, "HALF_OPEN admitted a second concurrent probe"
    assert concurrent.done()
    with pytest.raises(CircuitOpenError):
        await concurrent

    gate.set()
    with pytest.raises(RuntimeError):
        await probe

    assert breaker.state == CircuitState.OPEN

    # Still inside the fresh recovery window, so arrivals are rejected
    # rather than admitted as a second probe.
    with pytest.raises(CircuitOpenError):
        await breaker.call(_ok)

    # The probe slot did not leak. Asserted on what a caller can observe —
    # the next expired arrival is admitted and closes the circuit — not on
    # `_probe_in_flight`, which production writes on every exit path and
    # never reads: `call()` gates on `_state == HALF_OPEN` alone, so an
    # assertion on that field cannot fail for the reason it names.
    breaker._opened_at = breaker._opened_at - (breaker.recovery_timeout + 1)  # type: ignore[operator]
    assert await breaker.call(_ok) == "ok"
    assert breaker.state == CircuitState.CLOSED


async def test_probe_raising_circuit_open_error_does_not_deadlock(
    breaker: CircuitBreaker,
) -> None:
    """A nested breaker's rejection during a probe must not strand HALF_OPEN.

    `except CircuitOpenError: raise` deliberately passes through without
    counting the rejection as this breaker's own upstream failure, so it
    bypasses _on_success/_on_failure and must undo the probe itself.
    """
    await _open_and_expire(breaker)
    opened_at_before = breaker._opened_at

    async def _nested_rejected() -> str:
        raise CircuitOpenError("inner")

    with pytest.raises(CircuitOpenError):
        await breaker.call(_nested_rejected)

    assert breaker.state == CircuitState.OPEN
    # _opened_at is untouched, so the next caller may re-probe immediately
    # instead of waiting out another full recovery_timeout.
    assert breaker._opened_at == opened_at_before

    # The observable consequence, and the one this test exists for: the
    # very next caller is admitted rather than stranded behind a
    # HALF_OPEN the aborted probe never left.
    assert await breaker.call(_ok) == "ok"
    assert breaker.state == CircuitState.CLOSED
