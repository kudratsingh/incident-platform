"""Cross-module tripwire: `invalidate_cache_key`'s allowlist must actually
reach the cache keys the platform writes.

E2-02: the existing allowlist tests only ever checked accept/reject of
literal strings, so nothing noticed that the per-job read cache key
(`job:{tenant}:{id}`) matched no allowlisted prefix — an agent trying to
invalidate it got `cache_key_forbidden`. These tests import the real key
builders/constants from their owning modules, so a future rename breaks
the test instead of silently orphaning the tool again.
"""

import uuid

from app.mcp.tools.actions.invalidate_cache_key import _ALLOWED_PREFIXES
from app.mcp.tools.cache_key_info import _READABLE_PREFIXES
from app.mcp.tools.chaos.create_stale_cache import (
    _ALLOWED_PREFIXES as _CHAOS_ALLOWED_PREFIXES,
)
from app.mcp.tools.chaos.create_stale_cache import _DEFAULT_HOT_SET_KEY
from app.utils.backpressure import BACKPRESSURE_LAG_KEY
from app.utils.cache import JobCache


def _reachable(key: str) -> bool:
    return any(key.startswith(p) for p in _ALLOWED_PREFIXES)


def test_job_cache_key_is_reachable_by_invalidate_cache_key() -> None:
    """The assertion that would have caught E2-02."""
    key = JobCache._key(uuid.uuid4(), uuid.uuid4())
    assert _reachable(key), (
        f"JobCache key {key!r} matches no prefix in {list(_ALLOWED_PREFIXES)} — "
        "the MCP compensator cannot invalidate the per-job read cache"
    )


def test_backpressure_lag_key_is_reachable_by_invalidate_cache_key() -> None:
    assert _reachable(BACKPRESSURE_LAG_KEY), (
        f"{BACKPRESSURE_LAG_KEY!r} matches no prefix in {list(_ALLOWED_PREFIXES)}"
    )


def test_stale_cache_hot_set_key_is_reachable_by_invalidate_cache_key() -> None:
    assert _reachable(_DEFAULT_HOT_SET_KEY), (
        f"{_DEFAULT_HOT_SET_KEY!r} matches no prefix in {list(_ALLOWED_PREFIXES)}"
    )


def test_chaos_prefixes_are_a_subset_of_the_compensator_allowlist() -> None:
    """The round-trip invariant `create_stale_cache`'s docstring promises:
    anything the chaos hook can write, the compensator can delete."""
    assert set(_CHAOS_ALLOWED_PREFIXES) <= set(_ALLOWED_PREFIXES), (
        "create_stale_cache can write keys invalidate_cache_key refuses to "
        f"delete: {set(_CHAOS_ALLOWED_PREFIXES) - set(_ALLOWED_PREFIXES)}"
    )


def test_compensator_allowlist_is_a_subset_of_the_readable_prefixes() -> None:
    """Observe-what-you-can-delete: anything `invalidate_cache_key` may
    delete, `get_cache_key_info` must be able to inspect — before/after
    visibility is what makes a remediation verifiable."""
    assert set(_ALLOWED_PREFIXES) <= set(_READABLE_PREFIXES), (
        "invalidate_cache_key can delete keys get_cache_key_info cannot "
        f"observe: {set(_ALLOWED_PREFIXES) - set(_READABLE_PREFIXES)}"
    )


def test_chaos_prefixes_are_a_subset_of_the_readable_prefixes() -> None:
    """The acceptance invariant for `get_cache_key_info`: any key
    `create_stale_cache` can write must be observable through the read
    tool, or the fault it injects is invisible again."""
    assert set(_CHAOS_ALLOWED_PREFIXES) <= set(_READABLE_PREFIXES), (
        "create_stale_cache can write keys get_cache_key_info refuses to "
        f"read: {set(_CHAOS_ALLOWED_PREFIXES) - set(_READABLE_PREFIXES)}"
    )


def test_stale_cache_hot_set_key_is_readable_by_get_cache_key_info() -> None:
    assert any(_DEFAULT_HOT_SET_KEY.startswith(p) for p in _READABLE_PREFIXES), (
        f"{_DEFAULT_HOT_SET_KEY!r} matches no prefix in "
        f"{list(_READABLE_PREFIXES)} — the hot_set key cannot be observed"
    )
