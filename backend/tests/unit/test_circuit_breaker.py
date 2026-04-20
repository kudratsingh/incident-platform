import pytest

from app.core.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState


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
