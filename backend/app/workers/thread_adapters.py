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

"Between chunks" is where the work is offered to the publisher, not how often
it reaches Kafka: the publisher is wrapped in `progress.rate_limited`, so the
event count follows elapsed work rather than the caller's chunk_size.
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
    # Defensive clamps. The creation surfaces bound these (schemas.job.
    # CsvUploadPayload), but replays republish the stored payload without
    # revalidating, so pre-existing rows still reach us. chunk_size in
    # particular is a divisor on the next line and a range() step below —
    # chunk_size=0 raised ZeroDivisionError here and would have raised
    # ValueError from range(0, n, 0).
    row_count: int = max(0, min(int(payload.get("row_count", 500)), MAX_ROW_COUNT))
    chunk_size: int = max(1, min(int(payload.get("chunk_size", 100)), MAX_CHUNK_SIZE))
    total_chunks = max(1, (row_count + chunk_size - 1) // chunk_size)

    # Chunking is an I/O decision; it must not also decide how many Kafka
    # messages and immutable job_events rows this job writes. Unwrapped, that
    # count *was* `total_chunks` — caller-chosen, up to a million for one
    # upload (WO-R2-57). The floors leave the reporting a function of elapsed
    # work: first update, last update, and ~one per whole percent between.
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
