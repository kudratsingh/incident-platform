"""Tenant scoping for the cache tools (WO-R2-54).

The prefix allowlist says a key is a platform cache namespace, never that it is
*yours*. The tenant segment now comes from the authenticated principal, and both
tools call `assert_key_in_tenant` so the check cannot drift between read and delete.
"""

import uuid

from app.core.exceptions import AppError

#: Key families that embed a tenant id — only `cache:job:` today, where
#: `JobCache._key` puts the tenant in the segment after the prefix.
#: `tests/unit/test_cache_key_allowlist.py` derives a real key from it, so a
#: rename breaks a test, not the scoping.
_TENANT_SCOPED_PREFIXES = ("cache:job:",)


def tenant_segment(key: str) -> str | None:
    """The tenant segment of `key`, or `None` if this family has none."""
    for prefix in _TENANT_SCOPED_PREFIXES:
        if key.startswith(prefix):
            return key[len(prefix) :].split(":", 1)[0]
    return None


def job_segment(key: str) -> str | None:
    """The job segment of `key`, or `None` if this family has none.

    Sibling of `tenant_segment`: the shape of `cache:job:{tenant_id}:{job_id}`
    belongs in one module. Returns the raw segment, not a UUID — whether it parses
    is the caller's question.
    """
    for prefix in _TENANT_SCOPED_PREFIXES:
        if key.startswith(prefix):
            rest = key[len(prefix) :].split(":", 1)
            if len(rest) != 2 or not rest[1]:
                return None
            return rest[1]
    return None


def assert_key_in_tenant(
    key: str, *, tenant_id: uuid.UUID, error: type[AppError]
) -> None:
    """Refuse `key` unless its tenant segment is the caller's own tenant.

    `error` is the raising tool's own refusal class, so callers see the same
    `cache_key_forbidden` code the prefix gate gives. The message must not tell
    "wrong tenant" from "no such key" — that rebuilds the existence oracle.
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


__all__ = ["assert_key_in_tenant", "job_segment", "tenant_segment"]
