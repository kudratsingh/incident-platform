"""
`get_cache_key_info` — inspect one cache key's existence, TTL and shape.

The observability gap this closes: the platform's cache namespaces are
written by the read-through job cache, the metrics loop, and the
`create_stale_cache` chaos hook, but no read tool could see any of
those keys — `get_redis_health` deliberately reports aggregate stats
only, and `get_consumer_lag` reads exactly one key family. A caller
diagnosing a stale-cache condition had no way to confirm the key even
existed before firing `invalidate_cache_key`, nor to confirm the
delete afterwards. This tool is the missing read half of that
remediation loop.

Deliberately NOT an arbitrary-Redis-read primitive:

  - Exact key only — no patterns, no SCAN, no enumeration. ADR 0012's
    posture stays intact (`get_redis_health` "does not enumerate
    keys"); a caller must already know the key it wants to inspect.
  - Namespace allowlist — the same prefixes `invalidate_cache_key` may
    delete (mirror of its `_ALLOWED_PREFIXES`; the subset relations are
    asserted in `tests/unit/test_cache_key_allowlist.py`). Everything
    outside — rate-limit counters, queue ZSETs, read-model projections,
    progress snapshots, pause flags — is refused before any Redis call.
  - Shape only, never the value — `cache:job:{tenant}:{job_id}` payloads
    are tenant data. Existence / TTL / type / size answer the
    operational question ("is a stale entry sitting there?") without
    exposing them.
  - Tenant-scoped (R2-54) — withholding the payload was not enough on its
    own. Existence, TTL and size of another tenant's cached job is an
    existence oracle over their jobs, so the tenant segment of the key
    now has to be the calling principal's. See
    `app/mcp/tools/_cache_scope.py`; the same check guards
    `invalidate_cache_key`.

Requires `telemetry:read`.
"""

from typing import Any

from app.core.exceptions import AppError
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.mcp.tools._cache_scope import assert_key_in_tenant
from pydantic import BaseModel, ConfigDict, Field

# Mirror of `invalidate_cache_key._ALLOWED_PREFIXES` — observe-what-you-
# can-delete symmetry. Kept as a separate literal rather than an import
# because both tuples are BAKED VERBATIM into their tools' schemas and
# descriptions, so a change to either is deliberate contract drift that
# must show up in that file's own diff. The mirrors cannot drift
# silently: `tests/unit/test_cache_key_allowlist.py` asserts the
# compensator's allowlist (and, transitively, everything the
# `create_stale_cache` chaos hook can write) stays a subset of this
# tuple.
_READABLE_PREFIXES = (
    "cache:",
    "jobs:cache:",
    "kafka:consumer_lag:",
    "read_model:",
)

# Redis TYPE name → the size command that fits it. STRLEN counts bytes;
# the rest count elements. An unknown type name (a future Redis type)
# reports `size: null` rather than guessing.
_SIZE_COMMANDS = {
    "string": "strlen",
    "list": "llen",
    "set": "scard",
    "zset": "zcard",
    "hash": "hlen",
    "stream": "xlen",
}


class CacheKeyInfoError(AppError):
    status_code = 400
    # Deliberately the same refusal code as `invalidate_cache_key`: it is
    # the same allowlist decision, so callers can handle both uniformly.
    error_code = "cache_key_forbidden"


class GetCacheKeyInfoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(
        min_length=1,
        max_length=512,
        description=(
            "Exact Redis key to inspect. Must start with one of "
            f"{list(_READABLE_PREFIXES)}. Tenant-scoped keys "
            "(`cache:job:{tenant_id}:{job_id}`) are readable only within "
            "the calling principal's own tenant. Patterns are not "
            "supported — one exact key per call."
        ),
    )


class GetCacheKeyInfoOutput(BaseModel):
    key: str
    exists: bool
    type: str | None = Field(
        default=None,
        description="Redis value type — string, list, set, zset, hash, "
        "or stream. `null` when the key does not exist.",
    )
    ttl_seconds: int | None = Field(
        default=None,
        description="Remaining time-to-live in seconds. `null` when the "
        "key does not exist or has no expiry set — check `exists` to "
        "tell the two apart.",
    )
    size: int | None = Field(
        default=None,
        description="Value size: bytes (STRLEN) for string keys, element "
        "count for list / set / zset / hash / stream keys. `null` when "
        "the key does not exist.",
    )


@tool(
    "get_cache_key_info",
    description=(
        "Inspect one Redis cache key: existence, remaining TTL, value "
        "type, and size. Returns shape only — never the cached value, "
        "so tenant-scoped cache contents are not exposed.\n"
        "Restricted to the platform-owned cache namespaces "
        f"{list(_READABLE_PREFIXES)} — the same prefixes "
        "`invalidate_cache_key` may delete — so a suspect cache entry "
        "can be checked before remediation and confirmed gone after. "
        "Keys outside these namespaces are refused "
        "(`cache_key_forbidden`), as are tenant-scoped keys belonging to "
        "another tenant. Exact key only: this tool cannot enumerate "
        "keys, match patterns, or read arbitrary Redis state.\n"
        "FRESHNESS: live probe, measured at call time."
    ),
    input_model=GetCacheKeyInfoInput,
    output_model=GetCacheKeyInfoOutput,
    required_scope=Scope.TELEMETRY_READ,
)
async def get_cache_key_info(
    inp: GetCacheKeyInfoInput, ctx: ToolContext
) -> GetCacheKeyInfoOutput:
    if not any(inp.key.startswith(p) for p in _READABLE_PREFIXES):
        raise CacheKeyInfoError(
            f"Key {inp.key!r} is not under a readable namespace. "
            f"Allowed: {list(_READABLE_PREFIXES)}"
        )
    # Shape-only was never the whole answer (R2-54): existence, TTL and
    # size of `cache:job:{tenant}:{job}` are an existence oracle over
    # another tenant's jobs even with the payload withheld. Refused
    # before any Redis call, so the refusal cannot vary with what is
    # actually there.
    assert_key_in_tenant(
        inp.key, tenant_id=ctx.principal.tenant_id, error=CacheKeyInfoError
    )

    type_name = _as_str(await ctx.redis.type(inp.key))
    if type_name is None or type_name == "none":
        return GetCacheKeyInfoOutput(key=inp.key, exists=False)

    # redis-py convention: -2 missing (raced with expiry — treat the key
    # as present, it was a moment ago), -1 present with no expiry.
    ttl = _as_int(await ctx.redis.ttl(inp.key))
    ttl_seconds = ttl if ttl is not None and ttl >= 0 else None

    size: int | None = None
    size_command = _SIZE_COMMANDS.get(type_name)
    if size_command is not None:
        size = _as_int(await getattr(ctx.redis, size_command)(inp.key))

    return GetCacheKeyInfoOutput(
        key=inp.key,
        exists=True,
        type=type_name,
        ttl_seconds=ttl_seconds,
        size=size,
    )


def _as_str(v: Any) -> str | None:
    """TYPE returns `str` under `decode_responses=True` (production
    client) and `bytes` from a raw client — accept both, like the other
    tools' byte-safety."""
    if isinstance(v, bytes):
        return v.decode()
    if isinstance(v, str):
        return v
    return None


def _as_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


__all__ = [
    "CacheKeyInfoError",
    "GetCacheKeyInfoInput",
    "GetCacheKeyInfoOutput",
    "get_cache_key_info",
]
