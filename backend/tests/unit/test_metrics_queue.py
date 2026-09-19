"""The cardinality guards and the bounded emit queue in `app.core.metrics`."""

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import pytest
from app.core import metrics


@pytest.fixture(autouse=True)
def _isolated_metrics_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset the module globals around every test."""
    monkeypatch.setattr(metrics, "_is_production", lambda: True)
    metrics.reset_cardinality_state()
    monkeypatch.setattr(metrics, "_queue", None)
    monkeypatch.setattr(metrics, "_emitter_task", None)
    monkeypatch.setattr(metrics, "_dropped", 0)
    yield
    metrics.reset_cardinality_state()


def _dims(datum: metrics._Datum) -> dict[str, str]:
    return dict(datum.dimensions)


# Cardinality: the allow-list


def test_allow_list_rejects_a_value_outside_it() -> None:
    """A declared dimension takes declared values or `other` — nothing else."""
    metrics.register_dimension_values("Path", {"/jobs/{job_id}", "unmatched"})

    clean = metrics._sanitise_dimensions({"Path": "/jobs/{job_id}"})
    assert clean["Path"] == "/jobs/{job_id}", "a declared value must pass through"

    leaked = metrics._sanitise_dimensions(
        {"Path": "/api/v1/jobs/2f1c8a90-0000-4000-8000-000000000001"}
    )
    assert leaked["Path"] == metrics.OTHER_VALUE


def test_allow_list_is_additive_across_registrations() -> None:
    """Two routers registering separately must not clobber each other."""
    metrics.register_dimension_values("Path", {"/a"})
    metrics.register_dimension_values("Path", {"/b"})

    assert metrics._sanitise_dimensions({"Path": "/a"})["Path"] == "/a"
    assert metrics._sanitise_dimensions({"Path": "/b"})["Path"] == "/b"


# Cardinality: the hard cap for undeclared dimensions


def test_hard_cap_buckets_values_past_the_ceiling() -> None:
    """An undeclared dimension is bounded anyway."""
    for i in range(metrics.MAX_DIMENSION_VALUES):
        value = f"tenant-{i}"
        assert metrics._sanitise_dimensions({"Tenant": value})["Tenant"] == value

    # The ceiling is reached; every *new* value now collapses.
    for i in range(metrics.MAX_DIMENSION_VALUES, metrics.MAX_DIMENSION_VALUES + 25):
        clean = metrics._sanitise_dimensions({"Tenant": f"tenant-{i}"})
        assert clean["Tenant"] == metrics.OTHER_VALUE

    # ...but values seen before the cap still report themselves.
    assert metrics._sanitise_dimensions({"Tenant": "tenant-0"})["Tenant"] == "tenant-0"
    assert len(metrics._seen_values["Tenant"]) == metrics.MAX_DIMENSION_VALUES


def test_cap_is_per_dimension_not_global() -> None:
    """Filling one dimension's budget must not spend another's."""
    for i in range(metrics.MAX_DIMENSION_VALUES + 5):
        metrics._sanitise_dimensions({"Tenant": f"tenant-{i}"})

    assert metrics._sanitise_dimensions({"StatusCode": "200"})["StatusCode"] == "200"


# The bounded queue


async def test_queue_drops_on_overflow_instead_of_growing() -> None:
    """Overflow is a drop, not backpressure and not growth."""
    queue: asyncio.Queue[metrics._Datum] = asyncio.Queue(maxsize=3)
    metrics._queue = queue

    for i in range(10):
        await metrics.emit_gauge("RequestLatency", float(i), unit="Milliseconds")

    assert queue.qsize() == 3, "queue grew past its maxsize"
    assert metrics._dropped == 7


async def test_emit_is_a_noop_outside_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local dev and CI must never queue a datum or touch boto3."""
    monkeypatch.setattr(metrics, "_is_production", lambda: False)
    queue: asyncio.Queue[metrics._Datum] = asyncio.Queue(maxsize=10)
    metrics._queue = queue

    await metrics.emit_gauge("RequestLatency", 1.0, unit="Milliseconds")
    await metrics.emit_count("Whatever")

    assert queue.qsize() == 0


async def test_emit_without_a_running_emitter_drops_rather_than_leaks() -> None:
    """No emitter means nothing drains — retaining the datum would only leak."""
    await metrics.emit_gauge("RequestLatency", 1.0, unit="Milliseconds")
    assert metrics._dropped == 1


async def test_emit_does_not_wait_on_a_slow_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real 'emission does not block the request' guarantee."""
    def _glacial(_metric_data: list[dict[str, Any]]) -> None:
        time.sleep(30)  # would be catastrophic on the request path

    monkeypatch.setattr(metrics, "_put", _glacial)
    metrics._queue = asyncio.Queue(maxsize=1000)

    start = time.perf_counter()
    for _ in range(500):
        await metrics.emit_gauge(
            "RequestLatency", 12.5, unit="Milliseconds", dimensions={"Path": "/jobs"}
        )
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, f"500 emits took {elapsed:.2f}s — emit is doing I/O"


# Aggregation — the call-rate half of the finding


def test_aggregate_folds_samples_into_one_statistic_set() -> None:
    """N samples of one (metric, unit, dimensions) become one datum."""
    dims = (("Path", "/jobs/{job_id}"), ("StatusCode", "200"))
    batch = [
        metrics._Datum("RequestLatency", value, "Milliseconds", dims)
        for value in (10.0, 30.0, 20.0, 40.0)
    ]

    data = metrics._aggregate(batch)

    assert len(data) == 1, "one dimension combination must fold to one datum"
    (datum,) = data
    assert datum["MetricName"] == "RequestLatency"
    assert datum["Unit"] == "Milliseconds"
    assert datum["StatisticValues"] == {
        "SampleCount": 4.0,
        "Sum": 100.0,
        "Minimum": 10.0,
        "Maximum": 40.0,
    }
    assert datum["Dimensions"] == [
        {"Name": "Path", "Value": "/jobs/{job_id}"},
        {"Name": "StatusCode", "Value": "200"},
    ]


def test_aggregate_keeps_distinct_combinations_apart() -> None:
    """Folding must not merge different routes or different statuses."""
    batch = [
        metrics._Datum("RequestLatency", 10.0, "Milliseconds", (("Path", "/a"),)),
        metrics._Datum("RequestLatency", 20.0, "Milliseconds", (("Path", "/b"),)),
        metrics._Datum("RequestLatency", 30.0, "Milliseconds", (("Path", "/a"),)),
        metrics._Datum("QueueDepth", 5.0, "Count", (("Path", "/a"),)),
    ]

    data = metrics._aggregate(batch)

    assert len(data) == 3
    folded = {
        (d["MetricName"], d["Dimensions"][0]["Value"]): d["StatisticValues"]
        for d in data
    }
    assert folded[("RequestLatency", "/a")]["SampleCount"] == 2.0
    assert folded[("RequestLatency", "/a")]["Sum"] == 40.0
    assert folded[("RequestLatency", "/b")]["SampleCount"] == 1.0
    assert folded[("QueueDepth", "/a")]["SampleCount"] == 1.0


# Flushing


async def test_flush_makes_one_call_for_a_whole_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The call-rate fix, end to end: many requests, one PutMetricData."""
    calls: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(metrics, "_put", lambda data: calls.append(data))

    queue: asyncio.Queue[metrics._Datum] = asyncio.Queue(maxsize=5000)
    dims = (("Path", "/jobs/{job_id}"), ("StatusCode", "200"))
    for i in range(2000):
        queue.put_nowait(metrics._Datum("RequestLatency", float(i), "Milliseconds", dims))

    sent = await metrics._flush_once(queue)

    assert sent == 2000, "the whole window must be drained, not a slice of it"
    assert queue.qsize() == 0
    assert len(calls) == 1, f"2000 samples should cost one call, made {len(calls)}"
    assert calls[0][0]["StatisticValues"]["SampleCount"] == 2000.0


async def test_flush_chunks_only_when_distinct_datums_exceed_the_call_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`MAX_DATUMS_PER_CALL` bounds datums per API call, post-aggregation."""
    calls: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(metrics, "_put", lambda data: calls.append(data))

    queue: asyncio.Queue[metrics._Datum] = asyncio.Queue(maxsize=5000)
    distinct = metrics.MAX_DATUMS_PER_CALL + 20
    for i in range(distinct):
        queue.put_nowait(
            metrics._Datum("RequestLatency", 1.0, "Milliseconds", (("Path", f"/r{i}"),))
        )

    sent = await metrics._flush_once(queue)

    assert sent == distinct
    assert len(calls) == 2
    assert len(calls[0]) == metrics.MAX_DATUMS_PER_CALL
    assert len(calls[1]) == 20


async def test_flush_of_an_empty_queue_makes_no_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle service must not call CloudWatch every window for nothing."""
    calls: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(metrics, "_put", lambda data: calls.append(data))

    sent = await metrics._flush_once(asyncio.Queue(maxsize=10))

    assert sent == 0
    assert calls == []


async def test_stop_flushes_the_final_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shutdown must not discard up to a full interval of data."""
    calls: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(metrics, "_put", lambda data: calls.append(data))

    await metrics.start_metrics_emitter()
    await metrics.emit_gauge(
        "RequestLatency", 42.0, unit="Milliseconds", dimensions={"Path": "/jobs"}
    )
    await metrics.stop_metrics_emitter()

    assert len(calls) == 1
    assert calls[0][0]["StatisticValues"]["Sum"] == 42.0


async def test_sanitisation_happens_at_enqueue_not_at_flush() -> None:
    """The unbounded value must never reach the queue."""
    metrics.register_dimension_values("Path", {"/jobs/{job_id}"})
    queue: asyncio.Queue[metrics._Datum] = asyncio.Queue(maxsize=10)
    metrics._queue = queue

    await metrics.emit_gauge(
        "RequestLatency",
        1.0,
        unit="Milliseconds",
        dimensions={"Path": "/api/v1/jobs/2f1c8a90-dead-beef"},
    )

    assert _dims(queue.get_nowait())["Path"] == metrics.OTHER_VALUE
