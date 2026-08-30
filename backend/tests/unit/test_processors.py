"""Unit tests for all three job processor families."""

import asyncio
from concurrent.futures.process import BrokenProcessPool
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.core.circuit_breaker import CircuitState
from app.workers import cpu_processors, thread_adapters
from app.workers.async_tasks import (
    MAX_ENDPOINT_COUNT,
    _bulk_api_breaker,
    process_bulk_api_sync,
)
from app.workers.cpu_processors import (
    _analyze_document,
    _generate_report,
    _get_pool,
    _reset_pool,
    process_doc_analysis,
    process_report_gen,
)
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


async def test_csv_upload_event_count_is_bounded_not_the_chunk_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every progress publish is a Kafka message and an immutable job_events
    row. Unwrapped, a job wrote one of each per chunk — and chunk_size is the
    caller's, so a 1,000,000-row upload with chunk_size=1 wrote a million
    (WO-R2-57). The parse is stubbed out because the point under test is the
    publish rate, not the (simulated) blocking read.
    """
    monkeypatch.setattr(
        thread_adapters,
        "_parse_chunk_blocking",
        lambda start, end: {"chunk_start": start, "chunk_end": end, "checksum": 0},
    )
    log, publish = _collect_publishes()

    await process_csv_upload({"row_count": 20_000, "chunk_size": 1}, publish)

    # 20,000 chunks, at most one event per whole percent plus the first.
    assert len(log) <= 102
    assert log[0][0] == 0
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


# ---------------------------------------------------------------------------
# Defensive clamps (WO-P4-04 / E1-05)
#
# Request validation does not cover replays: JobService.replay_job republishes
# the stored payload, so rows created before the bounds existed still reach
# these functions. The clamps are the last line of defence.
# ---------------------------------------------------------------------------


async def test_bulk_api_sync_clamps_endpoint_count() -> None:
    """endpoint_count=5000 must not create 5000 tasks — the OOM vector of E1-05."""
    log, publish = _collect_publishes()
    created = 0
    real_create_task = asyncio.create_task

    def _counting_create_task(coro: Any, **kwargs: Any) -> Any:
        nonlocal created
        created += 1
        return real_create_task(coro, **kwargs)

    try:
        with patch.object(asyncio, "create_task", _counting_create_task):
            result = await process_bulk_api_sync({"endpoint_count": 5000}, publish)
    finally:
        # The breaker is a module-level singleton shared with every other test;
        # 100 simulated calls at a 10% failure rate reliably trip it.
        _bulk_api_breaker._state = CircuitState.CLOSED
        _bulk_api_breaker._failure_count = 0

    assert created <= MAX_ENDPOINT_COUNT
    assert result["total"] == MAX_ENDPOINT_COUNT
    assert log[-1][0] == 100


async def test_bulk_api_sync_zero_endpoints_does_not_divide_by_zero() -> None:
    log, publish = _collect_publishes()
    result = await process_bulk_api_sync({"endpoint_count": 0}, publish)
    assert result["total"] == 0
    assert result["endpoints_synced"] == 0
    assert result["results"] == []


def test_generate_report_clamps_zero_group_count() -> None:
    """group_count=0 silently produced an empty report at HEAD (the comprehension
    body — and its `row_count // group_count` — never ran), so the job "succeeded"
    with no groups. Clamping to >=1 also keeps that division safe."""
    result = _generate_report({"group_count": 0, "row_count": 10})
    assert result["groups"] == 1
    assert len(result["totals"]) == 1


def test_generate_report_clamps_oversized_counts() -> None:
    result = _generate_report({"group_count": 10**6, "row_count": 10})
    assert result["groups"] == 1000


def test_analyze_document_clamps_page_count_floor() -> None:
    """A negative page count is floored at 0, not passed to `range()`."""
    result = _analyze_document({"page_count": -5})
    assert result["pages_analyzed"] == 0


def test_analyze_document_clamps_page_count_ceiling() -> None:
    """The bound this function exists for, and the one that was untested.

    `page_count` is the loop count for per-page CPU work in a pool
    subprocess, so an unclamped value from a replayed payload is
    unbounded process-pool time. The test that claimed to pin the
    ceiling only ever passed `page_count=-5`, exercising the `max(0, …)`
    floor; `MAX_PAGE_COUNT` itself was never asserted (R2-64).

    Patched out to nothing so the assertion is about the clamp rather
    than about sitting through `MAX_PAGE_COUNT * 0.05s` of simulated
    work — which is also why the old test could not have covered this.
    """
    with patch.object(cpu_processors.time, "sleep", lambda _s: None):
        result = _analyze_document({"page_count": 5000})

    assert result["pages_analyzed"] == cpu_processors.MAX_PAGE_COUNT
    assert result["word_count"] == cpu_processors.MAX_PAGE_COUNT * 300


def test_analyze_document_page_count_below_the_ceiling_is_untouched() -> None:
    """The clamp is a ceiling, not a rewrite — a legal value passes through."""
    with patch.object(cpu_processors.time, "sleep", lambda _s: None):
        result = _analyze_document({"page_count": 7})
    assert result["pages_analyzed"] == 7


async def test_csv_upload_zero_chunk_size_does_not_raise() -> None:
    """chunk_size=0 raised ZeroDivisionError at HEAD (total_chunks divides by it,
    before range(0, n, 0) would have raised ValueError)."""
    log, publish = _collect_publishes()
    result = await process_csv_upload({"row_count": 2, "chunk_size": 0}, publish)
    assert result["chunk_size"] == 1
    assert result["total_rows"] == 2


# ---------------------------------------------------------------------------
# Process pool hardening (WO-P4-04 / E1-09)
# ---------------------------------------------------------------------------


def test_get_pool_uses_spawn_context_and_is_a_singleton() -> None:
    _reset_pool()
    try:
        pool, generation = _get_pool()
        assert _get_pool() == (pool, generation)
        assert pool._mp_context.get_start_method() == "spawn"
    finally:
        _reset_pool()


async def test_broken_process_pool_resets_pool_and_propagates() -> None:
    """A killed child poisoned every later CPU job at HEAD — the pool was never rebuilt."""
    log, publish = _collect_publishes()
    _reset_pool()
    original, _ = _get_pool()
    loop = asyncio.get_running_loop()

    async def _broken(*args: object, **kwargs: object) -> object:
        raise BrokenProcessPool("a child was killed")

    try:
        with patch.object(loop, "run_in_executor", side_effect=_broken):
            with pytest.raises(BrokenProcessPool):
                await process_doc_analysis({"page_count": 1}, publish)

        assert cpu_processors._process_pool is None

        # The next attempt (the dispatcher's retry) gets a fresh pool.
        assert _get_pool()[0] is not original
    finally:
        _reset_pool()


async def test_broken_process_pool_in_report_gen_resets_pool() -> None:
    log, publish = _collect_publishes()
    _reset_pool()
    _get_pool()
    loop = asyncio.get_running_loop()

    async def _broken(*args: object, **kwargs: object) -> object:
        raise BrokenProcessPool("a child was killed")

    try:
        with patch.object(loop, "run_in_executor", side_effect=_broken):
            with pytest.raises(BrokenProcessPool):
                await process_report_gen({"row_count": 10, "group_count": 2}, publish)

        assert cpu_processors._process_pool is None
    finally:
        _reset_pool()


# ---------------------------------------------------------------------------
# Concurrent pool resets (WO-R2-64)
#
# `_reset_pool` used to unconditionally drop whatever pool was current.
# When several CPU jobs failed on the same broken pool — the normal case,
# since a dead child breaks the pool for everyone at once — the first
# reset rebuilt it and the second tore the *replacement* down, taking
# any freshly submitted future with it (`cancel_futures=True`). The
# generation counter makes a reset apply only to the pool the caller
# actually observed as broken.
# ---------------------------------------------------------------------------


class _FakePool:
    """Records shutdowns without spawning real subprocesses."""

    built: list["_FakePool"] = []

    def __init__(self, **_kwargs: Any) -> None:
        self.shutdowns: list[bool] = []
        _FakePool.built.append(self)

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        self.shutdowns.append(cancel_futures)


@pytest.fixture
def fake_pools() -> Any:
    _reset_pool()
    _FakePool.built = []
    with patch.object(cpu_processors, "ProcessPoolExecutor", _FakePool):
        yield _FakePool
    _reset_pool()
    _FakePool.built = []


def test_stale_reset_spares_a_rebuilt_pool(fake_pools: Any) -> None:
    """The exact interleaving that used to cancel live work.

    Two jobs fail on pool P1. The first reset drops it; an unrelated job
    then rebuilds as P2 and submits work. The second failing job's reset
    still refers to P1 — it must be a no-op, not a teardown of P2."""
    p1, generation = _get_pool()

    _reset_pool(generation)  # first failure, against the pool it saw
    assert cpu_processors._process_pool is None
    assert p1.shutdowns == [True]

    p2, generation2 = _get_pool()  # unrelated job rebuilds and submits
    assert p2 is not p1
    assert generation2 != generation

    _reset_pool(generation)  # second failure, stale observation

    assert cpu_processors._process_pool is p2
    assert p2.shutdowns == []
    # Exactly one rebuild: P1 and P2, never a third.
    assert fake_pools.built == [p1, p2]


def test_reset_without_a_generation_is_still_unconditional(
    fake_pools: Any,
) -> None:
    """Teardown callers (tests, shutdown paths) that pass no generation
    keep the old drop-whatever-is-there behaviour."""
    p1, _ = _get_pool()
    _reset_pool()
    assert cpu_processors._process_pool is None
    assert p1.shutdowns == [True]


async def test_concurrent_broken_jobs_rebuild_the_pool_once(
    fake_pools: Any,
) -> None:
    """The same thing end-to-end: two CPU jobs awaiting the same broken
    pool, failing together, must leave exactly one rebuild behind."""
    _, publish = _collect_publishes()
    p1, _ = _get_pool()
    loop = asyncio.get_running_loop()

    async def _broken(*_args: object, **_kwargs: object) -> object:
        # Yield first, so both coroutines are past _get_pool() and
        # holding the same pool before either one fails.
        await asyncio.sleep(0)
        raise BrokenProcessPool("a child was killed")

    with patch.object(loop, "run_in_executor", side_effect=_broken):
        results = await asyncio.gather(
            process_doc_analysis({"page_count": 1}, publish),
            process_report_gen({"row_count": 1}, publish),
            return_exceptions=True,
        )

    assert all(isinstance(r, BrokenProcessPool) for r in results)
    # One teardown of the broken pool, and no pool built to replace it
    # behind a caller's back — the next job rebuilds lazily.
    assert p1.shutdowns == [True]
    assert fake_pools.built == [p1]
    assert cpu_processors._process_pool is None
