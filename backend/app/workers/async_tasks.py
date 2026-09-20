"""
asyncio job processors (bulk_api_sync) — N coroutines via `as_completed`, with `asyncio.sleep()`
standing in for network latency.
"""

import asyncio
import random
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.core.circuit_breaker import CircuitBreaker, CircuitOpenError, get_circuit_breaker
from app.core.logging import get_logger
from app.core.redis import get_redis_client
from app.core.tracing import get_tracer
from app.workers.progress import ProgressPublisher
from opentelemetry.trace import SpanKind

logger = get_logger(__name__)
tracer = get_tracer(__name__)

# Hard ceiling on eagerly-created tasks: replays republish a stored payload without revalidating,
# and the worker shares its process with the API, so an unbounded fan-out OOM-kills everything.
MAX_ENDPOINT_COUNT = 100

# One breaker per logical external service, shared across all jobs in this process.
_bulk_api_breaker = get_circuit_breaker(
    "bulk-api-sync",
    failure_threshold=3,
    recovery_timeout=30.0,
)

# `degrade_downstream` writes this key; this module reads it once per job (WO-R3-220). Under
# `chaos:*`, so the environment reset sweeps it with every other flag.
DOWNSTREAM_FLAG_KEY = "chaos:downstream:bulk_api_sync"

#: Endpoint calls raise, so the breaker counts them and opens at its threshold.
DEGRADE_FAIL = "fail"
#: Endpoint calls answer late but succeed, so the breaker stays closed.
DEGRADE_SLOW = "slow"


def bulk_api_breaker() -> CircuitBreaker:
    """The one registered breaker, so callers read its threshold rather than restating it."""
    return _bulk_api_breaker


def downstream_flag_key() -> str:
    """The key that degrades this processor's simulated endpoints."""
    return DOWNSTREAM_FLAG_KEY


@dataclass(frozen=True)
class Degradation:
    mode: str
    delay_ms: int


def parse_degradation(raw: Any) -> Degradation | None:
    """`"<mode>:<delay_ms>"` → a `Degradation`, or `None` when it is not one."""
    if raw is None:
        return None
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    mode, _, delay = text.partition(":")
    if mode not in (DEGRADE_FAIL, DEGRADE_SLOW):
        logger.warning("downstream flag names no known mode", extra={"value": text})
        return None
    try:
        delay_ms = int(delay)
    except ValueError:
        delay_ms = 0
    return Degradation(mode=mode, delay_ms=max(0, delay_ms))


async def read_degradation(redis: Any | None = None) -> Degradation | None:
    """What the flag asks for now: `None` when chaos is off, the key is absent, or Redis is down."""
    if not get_settings().chaos_enabled:
        return None
    client = redis if redis is not None else get_redis_client()
    try:
        raw = await client.get(DOWNSTREAM_FLAG_KEY)
    except Exception:
        # Fail open, like every other flag read: a Redis blip is not a fault to inject.
        logger.warning("downstream flag unreadable", exc_info=True)
        return None
    return parse_degradation(raw)


def _synced(index: int) -> dict[str, Any]:
    """One endpoint's successful answer."""
    return {
        "endpoint": index,
        "status": "ok",
        "records_synced": random.randint(10, 500),
    }


async def _degraded_call(degraded: Degradation, index: int) -> dict[str, Any]:
    """One endpoint call while the dependency is degraded: late, or a 503 the breaker counts."""
    if degraded.mode == DEGRADE_SLOW:
        await asyncio.sleep(degraded.delay_ms / 1000)
        return _synced(index)
    raise RuntimeError(f"endpoint {index} returned 503")


async def process_bulk_api_sync(
    payload: dict[str, Any],
    publish: ProgressPublisher,
) -> dict[str, Any]:
    """Call the payload's endpoints concurrently behind a circuit breaker,
    reporting progress as each one lands, and return per-endpoint results."""
    # One read per job, not per endpoint: the whole fan-out sees one dependency state.
    degraded = await read_degradation()
    requested_count: int = int(payload.get("endpoint_count", 5))
    endpoint_count: int = max(0, min(requested_count, MAX_ENDPOINT_COUNT))
    if endpoint_count != requested_count:
        logger.warning(
            "bulk_api_sync.endpoint_count clamped",
            extra={
                "requested_endpoint_count": requested_count,
                "endpoint_count": endpoint_count,
            },
        )
    await publish(0, f"Starting sync of {endpoint_count} endpoints")

    if endpoint_count == 0:
        # No endpoints means no progress denominator — publish the terminal
        # step explicitly rather than dividing by zero in the loop below.
        await publish(100, "No endpoints to sync")
        return {
            "endpoints_synced": 0,
            "errors": 0,
            "total": 0,
            "results": [],
        }

    async def _call_one(index: int) -> dict[str, Any]:
        with tracer.start_as_current_span(
            f"external.api_call/{index}", kind=SpanKind.CLIENT
        ) as span:
            span.set_attribute("endpoint.index", index)

            try:
                async def _do_call() -> dict[str, Any]:
                    if degraded is not None:
                        return await _degraded_call(degraded, index)
                    await asyncio.sleep(random.uniform(0.05, 0.3))
                    if random.random() < 0.10:
                        raise RuntimeError(f"endpoint {index} returned 503")
                    return _synced(index)

                result = await _bulk_api_breaker.call(_do_call)
                span.set_attribute("endpoint.status", "ok")
                return result

            except CircuitOpenError:
                span.set_attribute("endpoint.status", "circuit_open")
                return {"endpoint": index, "status": "error", "code": "circuit_open"}
            except Exception:
                span.set_attribute("endpoint.status", "error")
                return {"endpoint": index, "status": "error", "code": 503}

    tasks = [asyncio.create_task(_call_one(i)) for i in range(endpoint_count)]
    results: list[dict[str, Any]] = []

    for completed_count, future in enumerate(asyncio.as_completed(tasks), start=1):
        result = await future
        results.append(result)
        pct = int(completed_count / endpoint_count * 100)
        await publish(pct, f"Synced {completed_count}/{endpoint_count} endpoints")

    ok = sum(1 for r in results if r["status"] == "ok")
    errors = len(results) - ok
    if ok == 0:
        # A sync that synced nothing is a failed job, so the failure reaches the job surface —
        # retries, then the dead-letter queue — rather than completing with an error count no
        # operational tool reads.
        #
        # Unconditional since WO-R3-322 (owner decision O-31 D4): whatever made the endpoints
        # fail is not what decides whether a job that synced nothing succeeded. The flag still
        # chooses the injection (`fail` raises per call, `slow` answers late), never the
        # failure semantics. A partial failure is unchanged — still a completed job with its
        # errors counted. The SLO consequence is accepted rather than hidden: such a job
        # spends `job_completion_rate` budget once it dead-letters (ADR 0031, 2026-09-19
        # amendment).
        raise RuntimeError(
            f"bulk api sync failed: all {errors} endpoint calls failed "
            f"(0 of {endpoint_count} endpoints returned a result)"
        )
    return {
        "endpoints_synced": ok,
        "errors": errors,
        "total": endpoint_count,
        "results": results,
    }
