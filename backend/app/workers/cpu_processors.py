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

import time
from concurrent.futures import ProcessPoolExecutor
from typing import Any

from app.workers.progress import ProgressPublisher

# One process per CPU core is the right default for CPU-bound work.
# We cap at 4 to avoid overwhelming the host in constrained environments.
_process_pool = ProcessPoolExecutor(max_workers=4)


# ---------------------------------------------------------------------------
# Pure CPU functions — these run in worker processes, no I/O allowed
# ---------------------------------------------------------------------------


def _analyze_document(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Simulates CPU-intensive document analysis (text extraction, NLP).

    Real version: pdfplumber / pytesseract / spaCy — all CPU-bound and
    Python-GIL-limited.  Running in a subprocess bypasses the GIL entirely.
    """
    page_count: int = int(payload.get("page_count", 10))
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
    row_count: int = int(payload.get("row_count", 10_000))
    group_count: int = int(payload.get("group_count", 10))

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
    result: dict[str, Any] = await loop.run_in_executor(
        _process_pool, _analyze_document, payload
    )
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
    result: dict[str, Any] = await loop.run_in_executor(
        _process_pool, _generate_report, payload
    )
    await publish(
        100,
        f"Report generated — {result['groups']} groups, format={result['output_format']}",
    )
    return result
