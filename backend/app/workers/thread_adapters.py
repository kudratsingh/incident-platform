"""
Thread-based job processors (csv_upload) for blocking file I/O and SDKs that are not async-aware.
The blocking work is a sync function the wrapper calls via `run_in_executor`, and progress is
published between chunks because the async Redis client is not thread-safe. "Between chunks" is
only where work is offered — `progress.rate_limited` decides how often it reaches Kafka.
"""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.workers.progress import ProgressPublisher, rate_limited

# Module-level executor — reused across jobs, avoids repeated thread creation.
# A small pool is intentional: CSV parsing is memory-heavy; too many parallel
# parses would exhaust RAM before they exhaust CPU.
_thread_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="csv-worker")

# Mirror of the bounds enforced at the creation surfaces
# (schemas.job.CsvUploadPayload).
MAX_ROW_COUNT = 1_000_000
MAX_CHUNK_SIZE = 100_000


def _parse_chunk_blocking(chunk_start: int, chunk_end: int) -> dict[str, Any]:
    """Blocking CSV chunk parse, inside a worker thread. Simulated with `time.sleep` so the demo
    needs no real files."""
    time.sleep(0.08)  # simulate blocking I/O read
    rows_processed = chunk_end - chunk_start
    # Simulate some lightweight per-row processing
    checksum = sum(range(rows_processed)) % 65536
    return {"chunk_start": chunk_start, "chunk_end": chunk_end, "checksum": checksum}


async def process_csv_upload(
    payload: dict[str, Any],
    publish: ProgressPublisher,
) -> dict[str, Any]:
    """Parse a CSV in chunks, each chunk in a thread-pool worker so the event loop stays free."""
    # Defensive clamps: replays republish a stored payload without revalidating against
    # `schemas.job.CsvUploadPayload`, and chunk_size is both a divisor and a `range()` step here.
    row_count: int = max(0, min(int(payload.get("row_count", 500)), MAX_ROW_COUNT))
    chunk_size: int = max(1, min(int(payload.get("chunk_size", 100)), MAX_CHUNK_SIZE))
    total_chunks = max(1, (row_count + chunk_size - 1) // chunk_size)

    # Chunking is an I/O decision and must not also set how many Kafka messages and `job_events`
    # rows this job writes — unwrapped that count *was* `total_chunks` (WO-R2-57).
    publish = rate_limited(publish)

    await publish(0, f"Parsing {row_count} rows in chunks of {chunk_size}")

    loop = asyncio.get_running_loop()
    chunk_results: list[dict[str, Any]] = []

    for chunk_idx, chunk_start in enumerate(range(0, row_count, chunk_size)):
        chunk_end = min(chunk_start + chunk_size, row_count)
        result = await loop.run_in_executor(
            _thread_pool,
            _parse_chunk_blocking,
            chunk_start,
            chunk_end,
        )
        chunk_results.append(result)
        pct = int((chunk_idx + 1) / total_chunks * 100)
        await publish(pct, f"Parsed rows {chunk_start}–{chunk_end}")

    total_rows = sum(r["chunk_end"] - r["chunk_start"] for r in chunk_results)
    return {
        "total_rows": total_rows,
        "chunks_processed": len(chunk_results),
        "chunk_size": chunk_size,
    }
