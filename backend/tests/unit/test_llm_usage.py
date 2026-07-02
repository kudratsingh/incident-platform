"""Unit tests for the shared llm usage extractor."""

from unittest.mock import MagicMock

from app.services._llm_usage import extract_usage


def test_extracts_all_four_counters() -> None:
    response = MagicMock()
    response.usage = MagicMock(
        input_tokens=100,
        output_tokens=40,
        cache_creation_input_tokens=200,
        cache_read_input_tokens=1500,
    )
    assert extract_usage(response) == {
        "input_tokens": 100,
        "output_tokens": 40,
        "cache_creation_input_tokens": 200,
        "cache_read_input_tokens": 1500,
    }


def test_coerces_none_cache_fields_to_zero() -> None:
    """The SDK's `Usage.cache_read_input_tokens` is Optional[int] — `None`
    when the request had no cache activity. Persisting `None` is fine for
    JSONB, but breaks downstream `sum(usage.values())` aggregation. The
    helper coerces to 0.
    """
    response = MagicMock()
    response.usage = MagicMock(
        input_tokens=100,
        output_tokens=40,
        cache_creation_input_tokens=None,
        cache_read_input_tokens=None,
    )
    usage = extract_usage(response)
    assert usage["cache_creation_input_tokens"] == 0
    assert usage["cache_read_input_tokens"] == 0


def test_missing_cache_attrs_default_to_zero() -> None:
    """Older SDK versions may not expose the cache_* attributes at all.
    The `getattr(..., 0)` fallback covers that case."""
    class MinimalUsage:
        input_tokens = 100
        output_tokens = 40

    response = MagicMock()
    response.usage = MinimalUsage()
    usage = extract_usage(response)
    assert usage["cache_creation_input_tokens"] == 0
    assert usage["cache_read_input_tokens"] == 0
