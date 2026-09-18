"""
CPU-bound job processors (doc_analysis, report_gen) on a ProcessPoolExecutor, which escapes the
GIL where a thread would not. Progress is only reportable *between* submissions.

IMPORTANT: anything submitted must be module-level (picklable) and purely synchronous — no
asyncio, no SQLAlchemy, no Redis; plain dicts in, plain dicts out.
"""

import multiprocessing
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any

from app.core.logging import get_logger
from app.workers.progress import ProgressPublisher

logger = get_logger(__name__)

# One process per CPU core is the right default for CPU-bound work.
# We cap at 4 to avoid overwhelming the host in constrained environments.
_MAX_POOL_WORKERS = 4

# Lazy, not import-time: forking a thread-laden process risks children deadlocked on a lock no
# thread in the child holds, so a spawn context is pinned (~0.5-1s on the first CPU job); and one
# killed child breaks the pool permanently, so `_reset_pool()` drops it instead.
_process_pool: ProcessPoolExecutor | None = None

# Guards the (pool, generation) pair. A threading lock, held only around bookkeeping.
_pool_lock = threading.Lock()

# Monotonic id of the *current* pool, quoted back on a reset. Without it, a second job's reset tore
# down the healthy replacement the first had rebuilt, with its live work (WO-R2-64).
_pool_generation = 0


def _get_pool() -> tuple[ProcessPoolExecutor, int]:
    """Return the process pool and its generation, creating it on first use. They travel together
    so a reset must name the pool its caller saw fail."""
    global _process_pool, _pool_generation
    with _pool_lock:
        if _process_pool is None:
            _pool_generation += 1
            _process_pool = ProcessPoolExecutor(
                max_workers=_MAX_POOL_WORKERS,
                mp_context=multiprocessing.get_context("spawn"),
            )
        return _process_pool, _pool_generation


def _reset_pool(generation: int | None = None) -> None:
    """Discard the pool `generation` identified, so the next call rebuilds.

    A later current generation means someone already rebuilt, so this is a no-op; `None` drops
    whatever is current.
    """
    global _process_pool
    with _pool_lock:
        if _process_pool is None:
            return
        if generation is not None and generation != _pool_generation:
            logger.info(
                "cpu_processors.pool_reset_skipped",
                extra={
                    "observed_generation": generation,
                    "current_generation": _pool_generation,
                },
            )
            return
        pool, _process_pool = _process_pool, None

    # Outside the lock: shutdown can block, and nothing else may touch
    # this pool now that it is detached.
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception:  # pragma: no cover - shutdown of a broken pool is best effort
        logger.warning("cpu_processors.pool_shutdown_failed", exc_info=True)


# ---------------------------------------------------------------------------
# Pure CPU functions — these run in worker processes, no I/O allowed
# ---------------------------------------------------------------------------

# Mirror of the bounds enforced at the creation surfaces
# (schemas.job.DocAnalysisPayload / ReportGenPayload).
MAX_PAGE_COUNT = 1000
MAX_ROW_COUNT = 1_000_000
MAX_GROUP_COUNT = 1000


def _analyze_document(payload: dict[str, Any]) -> dict[str, Any]:
    """Simulates CPU-intensive document analysis; the real version is pdfplumber / spaCy."""
    # Clamped here, in the pure function, because it runs in the child and so
    # covers every caller — including replays, which republish a stored payload
    # without going back through request validation.
    page_count: int = max(0, min(int(payload.get("page_count", 10)), MAX_PAGE_COUNT))
    words_per_page = 300

    word_count = 0
    entity_count = 0
    for _ in range(page_count):
        time.sleep(0.05)  # simulate per-page CPU work
        # Simulate word counting
        fake_text = "word " * words_per_page
        word_count += len(fake_text.split())
        # Simulate NER pass
        entity_count += words_per_page // 20

    return {
        "pages_analyzed": page_count,
        "word_count": word_count,
        "entities_found": entity_count,
        "avg_words_per_page": words_per_page,
    }


def _generate_report(payload: dict[str, Any]) -> dict[str, Any]:
    """Simulates CPU-intensive report generation; really pandas + matplotlib."""
    # Same reasoning as _analyze_document. group_count needs a floor of 1, not
    # 0: it is the divisor in the aggregation below, and group_count=0 silently
    # produced a report with zero groups (the comprehension body never ran).
    row_count: int = max(0, min(int(payload.get("row_count", 10_000)), MAX_ROW_COUNT))
    group_count: int = max(1, min(int(payload.get("group_count", 10)), MAX_GROUP_COUNT))

    time.sleep(0.1)  # simulate data load
    # Simulate aggregation work
    totals = {f"group_{i}": sum(range(row_count // group_count)) for i in range(group_count)}
    time.sleep(0.1)  # simulate chart rendering

    return {
        "rows_processed": row_count,
        "groups": group_count,
        "totals": totals,
        "output_format": payload.get("format", "pdf"),
    }


# ---------------------------------------------------------------------------
# Async wrappers — submit to process pool, report progress around the boundary
# ---------------------------------------------------------------------------


async def process_doc_analysis(
    payload: dict[str, Any],
    publish: ProgressPublisher,
) -> dict[str, Any]:
    """Run doc analysis on the process pool, bracketed by before/after progress publishes."""
    import asyncio
    loop = asyncio.get_running_loop()

    await publish(5, "Submitting document analysis to process pool")
    pool, generation = _get_pool()
    try:
        result: dict[str, Any] = await loop.run_in_executor(
            pool, _analyze_document, payload
        )
    except BrokenProcessPool:
        # A child died, so the pool is permanently broken: drop it and re-raise into `_run_job`'s
        # retry path, scoped to our generation so a sibling's rebuilt pool survives.
        logger.warning("cpu_processors.pool_broken", extra={"processor": "doc_analysis"})
        _reset_pool(generation)
        raise
    await publish(
        100,
        f"Analysis complete — {result['word_count']} words across {result['pages_analyzed']} pages",
    )
    return result


async def process_report_gen(
    payload: dict[str, Any],
    publish: ProgressPublisher,
) -> dict[str, Any]:
    """Same reasoning as doc_analysis — aggregation + rendering is CPU-bound."""
    import asyncio
    loop = asyncio.get_running_loop()

    await publish(5, f"Generating report over {payload.get('row_count', 10000)} rows")
    pool, generation = _get_pool()
    try:
        result: dict[str, Any] = await loop.run_in_executor(
            pool, _generate_report, payload
        )
    except BrokenProcessPool:
        # See process_doc_analysis.
        logger.warning("cpu_processors.pool_broken", extra={"processor": "report_gen"})
        _reset_pool(generation)
        raise
    await publish(
        100,
        f"Report generated — {result['groups']} groups, format={result['output_format']}",
    )
    return result
