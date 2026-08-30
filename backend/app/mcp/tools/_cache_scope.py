"""Tenant scoping for the cache tools (WO-R2-54).

`invalidate_cache_key` and `get_cache_key_info` take an exact Redis key
from the caller and check it against a prefix allowlist. The allowlist
answers "is this a platform cache namespace?" — it never answered "is
this *your* cache entry?", so a service account in one tenant could evict
(a cross-tenant write) or probe the existence, TTL and payload size of (a
cross-tenant existence oracle) another tenant's cached job.

The fix is not a narrower allowlist. `cache:job:{tenant_id}:{job_id}` has
to stay reachable — force-refreshing a stale job read is the remediation
those tools exist for. What has to change is *whose* key the caller may
name: the tenant segment comes from the authenticated principal, and a
key whose segment says otherwise is refused before any Redis call.

Both tools call `assert_key_in_tenant` so the check cannot drift between
the one that reads and the one that deletes — the read tool is the
before/after half of the same remediation, and a scope gap in either is
the same leak.

Key families with no tenant segment (`cache:jobs:…:hot_set`,
`kafka:consumer_lag:*`, `read_model:*`) are platform-global: there is no
tenant to compare, and nothing of one tenant's to expose. They stay
reachable, gated by the allowlist alone as before.
"""

import uuid

from app.core.exceptions import AppError

#: Key families that embed a tenant id, and where in the key it sits.
#: Only `cache:job:` today — `app.utils.cache.JobCache._key` builds
#: `cache:job:{tenant_id}:{job_id}`, so the segment immediately after the
#: prefix is the tenant.
#:
#: Kept as a literal for the same reason the allowlists in the two tools
#: are, but `tests/unit/test_cache_key_allowlist.py` derives a real key
#: from `JobCache._key` and asserts this helper reads the right segment
#: out of it — so renaming the key shape breaks a test rather than
#: silently un-scoping the namespace.
_TENANT_SCOPED_PREFIXES = ("cache:job:",)


def tenant_segment(key: str) -> str | None:
    """The tenant segment of `key`, or `None` if this family has none."""
    for prefix in _TENANT_SCOPED_PREFIXES:
        if key.startswith(prefix):
            return key[len(prefix) :].split(":", 1)[0]
    return None


def assert_key_in_tenant(
    key: str, *, tenant_id: uuid.UUID, error: type[AppError]
) -> None:
    """Refuse `key` unless its tenant segment is the caller's own tenant.

    `error` is the raising tool's own refusal class, so a caller sees the
    same `cache_key_forbidden` code it already gets from the prefix gate —
    it is the same "this key is not yours to touch" decision, and both
    tools should be handleable uniformly (the precedent
    `get_cache_key_info` set against `invalidate_cache_key`).

    The message names the key the caller supplied and nothing else. It
    must not distinguish "wrong tenant" from "no such key": the tool is
    refusing to look, and a refusal that varied with what is in Redis
    would rebuild the existence oracle this closes.
    """
    segment = tenant_segment(key)
    if segment is None:
        return

    try:
        supplied = uuid.UUID(segment)
    except ValueError:
        raise error(
            f"Key {key!r} is not scoped to a tenant this principal can "
            "reach."
        ) from None

    if supplied != tenant_id:
        raise error(
            f"Key {key!r} is not scoped to a tenant this principal can "
            "reach."
        )


__all__ = ["assert_key_in_tenant", "tenant_segment"]
