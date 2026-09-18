"""
CloudWatch custom metrics — bounded, aggregated, off the request thread.

Namespace IncidentPlatform; no-op unless ENVIRONMENT=production. `emit_count` and
`emit_gauge` do **no I/O** — they sanitise dimensions and queue the datum; `_emitter_loop`
folds each window into one `StatisticSet` per (metric, unit, dimensions) for a single
`put_metric_data` call, and overflow drops rather than blocking a request. Cardinality is
bounded twice: an allow-list, then `MAX_DIMENSION_VALUES` distinct, then `OTHER_VALUE`.
"""

import asyncio
import logging
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

NAMESPACE = "IncidentPlatform"

# --- cardinality guards ----------------------------------------------------

#: Ceiling on distinct values for a dimension with no declared allow-list.
MAX_DIMENSION_VALUES = 100

#: Substituted for any dimension value that fails the allow-list or the cap.
OTHER_VALUE = "other"

# --- queue / flush tuning --------------------------------------------------

#: Bounded — a CloudWatch outage costs at most this many retained datums.
QUEUE_MAXSIZE = 10_000

#: One PutMetricData call per window. 60s matches the dispatcher metrics loop.
FLUSH_INTERVAL_SECONDS = 60.0

#: PutMetricData accepts 1000 datums per call; stay well under it so a single
#: call never trips the 40KB payload limit either.
MAX_DATUMS_PER_CALL = 500

_client: Any = None  # lazily initialised boto3 CloudWatch client


class _Datum(NamedTuple):
    metric_name: str
    value: float
    unit: str
    dimensions: tuple[tuple[str, str], ...]  # sorted, hashable


# Dimension name -> the finite set of values it may take. Absent means "no
# allow-list declared"; the hard cap applies instead.
_allowed_values: dict[str, frozenset[str]] = {}

# Dimension name -> distinct values seen so far, for the hard cap.
_seen_values: dict[str, set[str]] = {}

_queue: asyncio.Queue[_Datum] | None = None
_emitter_task: asyncio.Task[None] | None = None
_dropped: int = 0


def _get_client() -> Any:
    global _client
    if _client is None:
        import boto3  # type: ignore[import-untyped]

        _client = boto3.client("cloudwatch")
    return _client


def _is_production() -> bool:
    from app.config import get_settings

    return get_settings().environment == "production"


# ---------------------------------------------------------------------------
# Cardinality
# ---------------------------------------------------------------------------


def register_dimension_values(name: str, values: set[str] | frozenset[str]) -> None:
    """Declare the finite set of values a dimension may take; additive, so twice unions."""
    existing = _allowed_values.get(name, frozenset())
    _allowed_values[name] = existing | frozenset(values)


def reset_cardinality_state() -> None:
    """Clear allow-lists and the seen-value cap. For tests."""
    _allowed_values.clear()
    _seen_values.clear()


def _sanitise_dimensions(dimensions: dict[str, str]) -> dict[str, str]:
    """Apply the allow-list, then the hard cap. Never raises."""
    out: dict[str, str] = {}
    for name, raw in dimensions.items():
        value = str(raw)
        allowed = _allowed_values.get(name)
        if allowed is not None:
            if value not in allowed:
                logger.warning(
                    "metric dimension value not in allow-list; bucketed as %r",
                    OTHER_VALUE,
                    extra={"dimension": name, "value": value[:120]},
                )
                value = OTHER_VALUE
        else:
            seen = _seen_values.setdefault(name, set())
            if value not in seen:
                if len(seen) >= MAX_DIMENSION_VALUES:
                    logger.warning(
                        "metric dimension %r hit the %d-value cap; "
                        "bucketing further values as %r",
                        name,
                        MAX_DIMENSION_VALUES,
                        OTHER_VALUE,
                        extra={"dimension": name, "value": value[:120]},
                    )
                    value = OTHER_VALUE
                else:
                    seen.add(value)
        out[name] = value
    return out


# ---------------------------------------------------------------------------
# Enqueue side — called from request handlers and worker loops
# ---------------------------------------------------------------------------


def _enqueue(metric_name: str, value: float, unit: str, dimensions: dict[str, str]) -> None:
    """Non-blocking. Drops (and counts) rather than waiting for room."""
    global _dropped

    if _queue is None:
        # No emitter running — nothing consumes the queue, so retaining the
        # datum would only leak. Counted so the gap is visible.
        _dropped += 1
        return

    clean = _sanitise_dimensions(dimensions)
    datum = _Datum(
        metric_name=metric_name,
        value=float(value),
        unit=unit,
        dimensions=tuple(sorted(clean.items())),
    )
    try:
        _queue.put_nowait(datum)
    except asyncio.QueueFull:
        _dropped += 1
        if _dropped % 1000 == 1:
            logger.warning(
                "metric queue full; dropping datums",
                extra={"dropped_total": _dropped, "maxsize": QUEUE_MAXSIZE},
            )


async def emit_count(
    metric_name: str,
    value: float = 1.0,
    dimensions: dict[str, str] | None = None,
) -> None:
    """Increment a count metric by `value`. No-op outside production.

    Returns as soon as the datum is queued — never waits on CloudWatch.
    """
    if not _is_production():
        return
    _enqueue(metric_name, value, "Count", dimensions or {})


async def emit_gauge(
    metric_name: str,
    value: float,
    unit: str = "Count",
    dimensions: dict[str, str] | None = None,
) -> None:
    """Emit a point-in-time gauge metric. No-op outside production.

    Returns as soon as the datum is queued — never waits on CloudWatch.
    """
    if not _is_production():
        return
    _enqueue(metric_name, value, unit, dimensions or {})


# ---------------------------------------------------------------------------
# Drain side — one background task per process
# ---------------------------------------------------------------------------


def _aggregate(batch: list[_Datum]) -> list[dict[str, Any]]:
    """Fold a flush window into one StatisticSet per (metric, unit, dimensions).

    CloudWatch still reconstructs Average/Sum/Min/Max/Count.
    """
    folded: dict[tuple[str, str, tuple[tuple[str, str], ...]], dict[str, float]] = {}
    for d in batch:
        key = (d.metric_name, d.unit, d.dimensions)
        stat = folded.get(key)
        if stat is None:
            folded[key] = {
                "SampleCount": 1.0,
                "Sum": d.value,
                "Minimum": d.value,
                "Maximum": d.value,
            }
        else:
            stat["SampleCount"] += 1.0
            stat["Sum"] += d.value
            stat["Minimum"] = min(stat["Minimum"], d.value)
            stat["Maximum"] = max(stat["Maximum"], d.value)

    return [
        {
            "MetricName": metric_name,
            "Dimensions": [{"Name": k, "Value": v} for k, v in dims],
            "StatisticValues": stat,
            "Unit": unit,
        }
        for (metric_name, unit, dims), stat in folded.items()
    ]


def _put(metric_data: list[dict[str, Any]]) -> None:
    """Synchronous CloudWatch PutMetricData call — runs inside a thread executor."""
    try:
        _get_client().put_metric_data(Namespace=NAMESPACE, MetricData=metric_data)
    except Exception:
        logger.warning(
            "Failed to emit %d CloudWatch datums", len(metric_data), exc_info=True
        )


async def _flush_once(queue: asyncio.Queue[_Datum]) -> int:
    """Drain everything currently queued and ship it. Returns samples sent.

    The whole queue drains, not a slice: `MAX_DATUMS_PER_CALL` bounds one API
    call, and aggregation collapses the window first. Capping the drain would
    throttle the consumer below the producer; `QUEUE_MAXSIZE` is the bound.
    """
    batch: list[_Datum] = []
    while True:
        try:
            batch.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break

    if not batch:
        return 0

    metric_data = _aggregate(batch)
    loop = asyncio.get_running_loop()
    for start in range(0, len(metric_data), MAX_DATUMS_PER_CALL):
        chunk = metric_data[start : start + MAX_DATUMS_PER_CALL]
        await loop.run_in_executor(None, _put, chunk)
    return len(batch)


async def _emitter_loop(queue: asyncio.Queue[_Datum]) -> None:
    """Flush the queue on a fixed interval until cancelled; errors are swallowed."""
    while True:
        try:
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
            await _flush_once(queue)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("metrics flush failed", exc_info=True)


async def start_metrics_emitter() -> None:
    """Start the background flush task. Idempotent; no-op outside production."""
    global _queue, _emitter_task

    if not _is_production():
        return
    if _emitter_task is not None and not _emitter_task.done():
        return

    _queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
    _emitter_task = asyncio.create_task(_emitter_loop(_queue))
    logger.info(
        "metrics emitter started",
        extra={"flush_seconds": FLUSH_INTERVAL_SECONDS, "maxsize": QUEUE_MAXSIZE},
    )


async def stop_metrics_emitter() -> None:
    """Cancel the flush task after one last flush, so a clean shutdown does not
    silently discard the final window."""
    global _queue, _emitter_task

    task, queue = _emitter_task, _queue
    _emitter_task, _queue = None, None

    if task is None:
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    if queue is not None:
        try:
            await _flush_once(queue)
        except Exception:
            logger.warning("final metrics flush failed", exc_info=True)

    if _dropped:
        logger.warning("metrics dropped over process lifetime", extra={"dropped": _dropped})
