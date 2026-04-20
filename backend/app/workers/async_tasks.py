"""
asyncio-based job processors.

Used for: bulk_api_sync — high-concurrency I/O where we want many in-flight
operations at once without blocking the event loop.

Design: fire off N coroutines with asyncio.gather / as_completed, report
progress as each one resolves.  asyncio.sleep() simulates the actual network
latency of real API calls.
"""

import asyncio
import random
from typing import Any

from app.core.circuit_breaker import CircuitOpenError, get_circuit_breaker
from app.core.tracing import get_tracer
from app.workers.progress import ProgressPublisher
from opentelemetry.trace import SpanKind

tracer = get_tracer(__name__)

# One breaker per logical external service, shared across all jobs in this process.
_bulk_api_breaker = get_circuit_breaker(
    "bulk-api-sync",
    failure_threshold=3,
    recovery_timeout=30.0,
)


async def process_bulk_api_sync(
    payload: dict[str, Any],
    publish: ProgressPublisher,
) -> dict[str, Any]:
    endpoint_count: int = int(payload.get("endpoint_count", 5))
    await publish(0, f"Starting sync of {endpoint_count} endpoints")

    async def _call_one(index: int) -> dict[str, Any]:
        with tracer.start_as_current_span(
            f"external.api_call/{index}", kind=SpanKind.CLIENT
        ) as span:
            span.set_attribute("endpoint.index", index)

            try:
                async def _do_call() -> dict[str, Any]:
                    await asyncio.sleep(random.uniform(0.05, 0.3))
                    if random.random() < 0.10:
                        raise RuntimeError(f"endpoint {index} returned 503")
                    return {
                        "endpoint": index,
                        "status": "ok",
                        "records_synced": random.randint(10, 500),
                    }

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
    return {
        "endpoints_synced": ok,
        "errors": errors,
        "total": endpoint_count,
        "results": results,
    }
