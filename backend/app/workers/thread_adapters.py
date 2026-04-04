"""
Thread-based job processors.

Used for: csv_upload — wraps blocking file I/O / legacy SDKs that aren't
async-aware.  Running these on the event loop thread would stall all other
coroutines; a ThreadPoolExecutor keeps the loop free.

Design: the blocking work is a plain synchronous function.  The async wrapper
calls it via loop.run_in_executor so the event loop can do other work while
the thread is blocked.  Progress is reported between chunks — we can't publish
from inside the thread itself (Redis client isn't thread-safe in async mode),
so we chunk the work and publish from the async wrapper between chunks.
"""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.workers.progress import ProgressPublisher

# Module-level executor — reused across jobs, avoids repeated thread creation.
# A small pool is intentional: CSV parsing is memory-heavy; too many parallel
# parses would exhaust RAM before they exhaust CPU.
_thread_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="csv-worker")


def _parse_chunk_blocking(chunk_start: int, chunk_end: int) -> dict[str, Any]:
    """
    Blocking CSV chunk parse — runs inside a worker thread.

    In a real implementation this would use csv.reader / pandas on a file
    object, which makes blocking read() syscalls.  We simulate that with
    time.sleep so the demo is runnable without real files.
    """
    time.sleep(0.08)  # simulate blocking I/O read
    rows_processed = chunk_end - chunk_start
    # Simulate some lightweight per-row processing
    checksum = sum(range(rows_processed)) % 65536
    return {"chunk_start": chunk_start, "chunk_end": chunk_end, "checksum": checksum}


async def process_csv_upload(
    payload: dict[str, Any],
    publish: ProgressPublisher,
) -> dict[str, Any]:
    """
    Parses a CSV in chunks, each chunk in a thread-pool worker.

    Why threads here: the CSV library and file objects are blocking and not
    async-aware.  run_in_executor offloads each blocking chunk-read to a thread
    while the event loop stays responsive for other requests.
    """
    row_count: int = int(payload.get("row_count", 500))
    chunk_size: int = int(payload.get("chunk_size", 100))
    total_chunks = max(1, (row_count + chunk_size - 1) // chunk_size)

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
