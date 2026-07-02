"""
Shared helper for extracting the cost-telemetry `usage` dict from an
Anthropic API response.

Every LLM service in this codebase persists the same four counters to its
`usage` JSONB column so cache-hit rates are visible over time. Extracting
those counters is fiddly enough to be worth centralizing:

  * `input_tokens` / `output_tokens` are always present.
  * `cache_read_input_tokens` and `cache_creation_input_tokens` are
    Optional[int] on the SDK's Usage type — they're `None` (not 0) when
    the request had no cache activity. Persisting `None` is fine for
    JSONB but breaks any downstream `sum()` aggregation. Coerce to 0
    here so callers can treat them as counters.

Small enough that inlining would be fine, but four sites doing the exact
same coercion is one site too many.
"""

from typing import Any


def extract_usage(response: Any) -> dict[str, int]:
    """Return the four-counter usage dict from an anthropic response.

    Coerces `None` (cache-miss default on the SDK) to 0 so downstream
    aggregations don't have to handle two representations.
    """
    usage = response.usage
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
    }
