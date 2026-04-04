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

from app.workers.progress import ProgressPublisher


async def process_bulk_api_sync(
    payload: dict[str, Any],
    publish: ProgressPublisher,
) -> dict[str, Any]:
    """
    Concurrently "calls" N external API endpoints and aggregates results.

    Why asyncio here: each call is I/O-bound and we want all of them in-flight
    simultaneously.  A thread per call would waste OS resources at scale.
    asyncio.gather gives us fan-out with a single thread.
    """
    endpoint_count: int = int(payload.get("endpoint_count", 5))
    await publish(0, f"Starting sync of {endpoint_count} endpoints")

    async def _call_one(index: int) -> dict[str, Any]:
        # Simulate variable network latency
        await asyncio.sleep(random.uniform(0.05, 0.3))
        # Simulate occasional transient errors (10% chance)
        if random.random() < 0.10:
            return {"endpoint": index, "status": "error", "code": 503}
        return {"endpoint": index, "status": "ok", "records_synced": random.randint(10, 500)}

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
