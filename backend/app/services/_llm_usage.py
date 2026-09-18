"""
Shared helper for the cost-telemetry `usage` dict on an Anthropic response.

Every LLM service persists the same four counters to its `usage` JSONB column.
`cache_read_input_tokens` and `cache_creation_input_tokens` are Optional[int] on the SDK's
Usage type — `None`, not 0, with no cache activity, which breaks a downstream `sum()`, so
they are coerced to 0 here.
"""

from typing import Any


def extract_usage(response: Any) -> dict[str, int]:
    """Return the four-counter usage dict, with cache-miss `None` coerced to 0."""
    usage = response.usage
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
    }
