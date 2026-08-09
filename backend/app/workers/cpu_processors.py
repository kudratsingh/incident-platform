"""
CPU-bound job processors using a ProcessPoolExecutor.

Used for: doc_analysis, report_gen — work that is genuinely CPU-intensive
(text extraction, aggregation, PDF rendering).  Running this on the event
loop or even in a thread would peg a single CPU core and starve the GIL.
A separate process gets its own GIL and its own CPU core.

IMPORTANT: Functions submitted to ProcessPoolExecutor must be:
  - Defined at module level (picklable)
  - Purely synchronous — no asyncio, no SQLAlchemy, no Redis
  - Self-contained — they receive plain dicts, return plain dicts

Progress can only be reported *between* process submissions, not from
inside the subprocess.  For fine-grained progress, split work into
multiple smaller process submissions.
"""

import multiprocessing
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

# The pool is created lazily rather than at import time, for two reasons:
#
#  1. Start method. On Linux + CPython, multiprocessing defaults to fork. A
#     pool built at import time forks whatever the process looks like *then*;
#     by the time a CPU job runs, thread_adapters' csv-worker ThreadPoolExecutor
#     and the Kafka/Redis clients are live, and forking a thread-laden process
#     risks children that deadlock on a lock held by a thread that does not
#     exist in the child. We pin an explicit spawn context instead, which costs
#     ~0.5-1s of child startup on the first CPU job and nothing after.
#     (macOS already defaults to spawn, which is why tests never saw this.)
#
#  2. Recovery. If a child is killed — OOM killer, ECS task pressure — the pool
#     enters a permanently broken state and every subsequent submit raises
#     BrokenProcessPool. With a module-level singleton that state survived until
#     the task was restarted, so one dead child failed every later CPU job.
#     _reset_pool() drops the broken pool so the next attempt rebuilds it.
_process_pool: ProcessPoolExecutor | None = None


def _get_pool() -> ProcessPoolExecutor:
    """Return the process pool, creating it on first use."""
    global _process_pool
    if _process_pool is None:
        _process_pool = ProcessPoolExecutor(
            max_workers=_MAX_POOL_WORKERS,
            mp_context=multiprocessing.get_context("spawn"),
        )
    return _process_pool


def _reset_pool() -> None:
    """Discard the current pool (best effort) so the next call builds a fresh one."""
    global _process_pool
    pool, _process_pool = _process_pool, None
    if pool is None:
        return
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
    """
    Simulates CPU-intensive document analysis (text extraction, NLP).

    Real version: pdfplumber / pytesseract / spaCy — all CPU-bound and
    Python-GIL-limited.  Running in a subprocess bypasses the GIL entirely.
    """
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
    """
    Simulates CPU-intensive report generation (data aggregation, chart rendering).

    Real version: pandas aggregations + matplotlib/reportlab — CPU-bound.
    """
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
    """
    Why a process pool here: pdfplumber/spaCy are CPU-bound and GIL-bound.
    A thread would not help — only a separate process escapes the GIL.
    We lose the ability to report granular progress from inside the process,
    so we bracket with before/after publishes.
    """
    import asyncio
    loop = asyncio.get_running_loop()

    await publish(5, "Submitting document analysis to process pool")
    try:
        result: dict[str, Any] = await loop.run_in_executor(
            _get_pool(), _analyze_document, payload
        )
    except BrokenProcessPool:
        # A child died (OOM kill, host pressure). The pool is permanently
        # broken; drop it and re-raise so _run_job's normal retry path picks
        # this up — the next attempt builds a fresh pool instead of failing
        # every CPU job from here until the task restarts.
        logger.warning("cpu_processors.pool_broken", extra={"processor": "doc_analysis"})
        _reset_pool()
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
    try:
        result: dict[str, Any] = await loop.run_in_executor(
            _get_pool(), _generate_report, payload
        )
    except BrokenProcessPool:
        # See process_doc_analysis.
        logger.warning("cpu_processors.pool_broken", extra={"processor": "report_gen"})
        _reset_pool()
        raise
    await publish(
        100,
        f"Report generated — {result['groups']} groups, format={result['output_format']}",
    )
    return result
