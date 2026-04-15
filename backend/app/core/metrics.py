"""
CloudWatch custom metrics — fire-and-forget async wrapper.

Only emits in production (ENVIRONMENT=production). In all other environments
calls are no-ops so local dev and CI are unaffected and boto3 is never invoked.

Emit calls run in a thread executor because boto3 is synchronous. Errors are
logged and swallowed — a metrics failure must never take down the main path.

Namespace: IncidentPlatform
"""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

NAMESPACE = "IncidentPlatform"

_client: Any = None  # lazily initialised boto3 CloudWatch client


def _get_client() -> Any:
    global _client
    if _client is None:
        import boto3  # type: ignore[import-untyped]

        _client = boto3.client("cloudwatch")
    return _client


def _put(
    metric_name: str,
    value: float,
    unit: str,
    dimensions: dict[str, str],
) -> None:
    """Synchronous CloudWatch PutMetricData call — runs inside a thread executor."""
    try:
        _get_client().put_metric_data(
            Namespace=NAMESPACE,
            MetricData=[
                {
                    "MetricName": metric_name,
                    "Dimensions": [{"Name": k, "Value": v} for k, v in dimensions.items()],
                    "Value": value,
                    "Unit": unit,
                }
            ],
        )
    except Exception:
        logger.warning("Failed to emit CloudWatch metric %s", metric_name, exc_info=True)


def _is_production() -> bool:
    from app.config import get_settings

    return get_settings().environment == "production"


async def emit_count(
    metric_name: str,
    value: float = 1.0,
    dimensions: dict[str, str] | None = None,
) -> None:
    """Increment a count metric by `value`. No-op outside production."""
    if not _is_production():
        return
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _put, metric_name, value, "Count", dimensions or {})


async def emit_gauge(
    metric_name: str,
    value: float,
    unit: str = "Count",
    dimensions: dict[str, str] | None = None,
) -> None:
    """Emit a point-in-time gauge metric. No-op outside production."""
    if not _is_production():
        return
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _put, metric_name, value, unit, dimensions or {})
