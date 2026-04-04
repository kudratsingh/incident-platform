"""Unit tests for all three job processor families."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.workers.async_tasks import process_bulk_api_sync
from app.workers.cpu_processors import _analyze_document, _generate_report, process_doc_analysis, process_report_gen
from app.workers.thread_adapters import process_csv_upload


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_publishes() -> tuple[list[tuple[int, str]], AsyncMock]:
    """Returns (log, publish_mock) where log accumulates (progress, message) calls."""
    log: list[tuple[int, str]] = []

    async def _publish(pct: int, msg: str) -> None:
        log.append((pct, msg))

    return log, _publish  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# async_tasks
# ---------------------------------------------------------------------------


async def test_bulk_api_sync_returns_summary() -> None:
    log, publish = _collect_publishes()
    result = await process_bulk_api_sync({"endpoint_count": 3}, publish)

    assert result["total"] == 3
    assert result["endpoints_synced"] + result["errors"] == 3
    assert len(log) > 0  # progress was published
    # Final progress call should be 100%
    assert log[-1][0] == 100


async def test_bulk_api_sync_publishes_incremental_progress() -> None:
    log, publish = _collect_publishes()
    await process_bulk_api_sync({"endpoint_count": 4}, publish)
    # Progress values should be strictly increasing (ignoring the first 0)
    pcts = [p for p, _ in log]
    assert pcts == sorted(pcts)


# ---------------------------------------------------------------------------
# thread_adapters
# ---------------------------------------------------------------------------


async def test_csv_upload_returns_row_count() -> None:
    log, publish = _collect_publishes()
    result = await process_csv_upload({"row_count": 200, "chunk_size": 100}, publish)

    assert result["total_rows"] == 200
    assert result["chunks_processed"] == 2
    assert len(log) >= 2


async def test_csv_upload_progress_reaches_100() -> None:
    log, publish = _collect_publishes()
    await process_csv_upload({"row_count": 100, "chunk_size": 100}, publish)
    assert log[-1][0] == 100


# ---------------------------------------------------------------------------
# cpu_processors — pure functions (no executor, run synchronously in tests)
# ---------------------------------------------------------------------------


def test_analyze_document_returns_word_count() -> None:
    result = _analyze_document({"page_count": 2})
    assert result["pages_analyzed"] == 2
    assert result["word_count"] > 0
    assert result["entities_found"] > 0


def test_generate_report_returns_groups() -> None:
    result = _generate_report({"row_count": 100, "group_count": 5})
    assert result["groups"] == 5
    assert result["rows_processed"] == 100
    assert len(result["totals"]) == 5


async def test_doc_analysis_async_wrapper() -> None:
    log, publish = _collect_publishes()

    # Patch run_in_executor so it calls the function synchronously (no real process)
    loop = asyncio.get_running_loop()

    async def _sync_executor(executor: object, fn: object, *args: object) -> object:
        import inspect
        if callable(fn):
            return fn(*args)
        return {}

    with patch.object(loop, "run_in_executor", side_effect=_sync_executor):
        result = await process_doc_analysis({"page_count": 1}, publish)

    assert "pages_analyzed" in result
    assert result["word_count"] > 0
    assert len(log) >= 2


async def test_report_gen_async_wrapper() -> None:
    log, publish = _collect_publishes()

    loop = asyncio.get_running_loop()

    async def _sync_executor(executor: object, fn: object, *args: object) -> object:
        if callable(fn):
            return fn(*args)
        return {}

    with patch.object(loop, "run_in_executor", side_effect=_sync_executor):
        result = await process_report_gen({"row_count": 100, "group_count": 2}, publish)

    assert "rows_processed" in result
    assert result["groups"] == 2
    assert len(log) >= 2
