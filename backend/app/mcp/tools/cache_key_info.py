"""
`get_cache_key_info` — existence, TTL, shape and record references for one cache key.
The read half of `invalidate_cache_key`: exact key only, namespace allowlist,
tenant-scoped (R2-54), never the value. `records_referenced` / `records_found` are
the staleness evidence (WO-R3-267): null, never zero, when unreadable.
Requires `telemetry:read`.
"""

import json
import uuid
from typing import Any

from app.core.db_degrade import degrade_on_db_error
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.mcp.tools._cache_scope import assert_key_in_tenant, job_segment
from app.repositories.job import JobRepository
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)

# Mirror of `invalidate_cache_key._ALLOWED_PREFIXES` — observe what you can delete.
# A separate literal, not an import: both tuples are BAKED VERBATIM into their
# tools' schemas. `tests/unit/test_cache_key_allowlist.py` keeps the mirror honest.
_READABLE_PREFIXES = (
    "cache:",
    "jobs:cache:",
    "kafka:consumer_lag:",
    "read_model:",
)

# Redis TYPE → size command; an unknown type reports `size: null`, not a guess.
_SIZE_COMMANDS = {
    "string": "strlen",
    "list": "llen",
    "set": "scard",
    "zset": "zcard",
    "hash": "hlen",
    "stream": "xlen",
}

# Most references the record check resolves in one call. Beyond it the tool declines
# rather than report a partial count as a whole one; the largest list any writer of
# these namespaces produces is 100 entries.
_MAX_REFERENCES_CHECKED = 500


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
    records_referenced: int | None = Field(
        default=None,
        description="How many job records this entry is meant to name — "
        "the items in its stored list, or the single job its key names. "
        "`null` when the platform could not work out what records the "
        "entry refers to (a counter, a collection type, an unreadable "
        "value, or a list too long to check), and `null` when the key "
        "does not exist. Not a synonym for 0: 0 means an entry that "
        "names no records at all.",
    )
    records_found: int | None = Field(
        default=None,
        description="How many of those the database holds right now, in "
        "your own tenant, looked up at call time. Below "
        "`records_referenced` means this cached copy points at records "
        "the platform does not have. Equal to it means every reference "
        "resolves — which is not a claim that the copied fields are "
        "current. `null` exactly when `records_referenced` is null.",
    )


@tool(
    "get_cache_key_info",
    description=(
        "Inspect one Redis cache key: existence, remaining TTL, value "
        "type, size, and whether the records it refers to still exist. "
        "Returns shape only — never the cached value, so tenant-scoped "
        "cache contents are not exposed.\n"
        "Restricted to the platform-owned cache namespaces "
        f"{list(_READABLE_PREFIXES)} — the same prefixes "
        "`invalidate_cache_key` may delete — so a suspect cache entry "
        "can be checked before remediation and confirmed gone after. "
        "Keys outside these namespaces are refused "
        "(`cache_key_forbidden`), as are tenant-scoped keys belonging to "
        "another tenant. Exact key only: this tool cannot enumerate "
        "keys, match patterns, or read arbitrary Redis state.\n"
        "THE RECORD CHECK. Entries in these namespaces are copies of job "
        "records the database holds. `records_referenced` is how many "
        "job records this entry is meant to name — the items in its "
        "stored list, or the single job its key names. `records_found` "
        "is how many of those the database has right now, in your own "
        "tenant. Fewer found than referenced means this copy points at "
        "records the platform does not have; equal counts mean every "
        "reference resolves, which is not a claim that the copied "
        "fields are current. Both are null when the platform could not "
        "work out what records the entry refers to — null is not zero.\n"
        "FRESHNESS: live probe, measured at call time; the record check "
        "queries the database on every call."
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
    # Shape alone was never enough (R2-54): existence, TTL and size are an
    # existence oracle. Refused before any Redis call.
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

    referenced, found = await resolve_record_references(
        inp.key, type_name, ctx=ctx
    )

    return GetCacheKeyInfoOutput(
        key=inp.key,
        exists=True,
        type=type_name,
        ttl_seconds=ttl_seconds,
        size=size,
        records_referenced=referenced,
        records_found=found,
    )


async def resolve_record_references(
    key: str, type_name: str, *, ctx: ToolContext
) -> tuple[int | None, int | None]:
    """`(records_referenced, records_found)` for an existing key.

    Public because `tests/unit/test_eval_reset.py` asks this same question of the
    hot-set entry the reset re-populates. Both halves are null together — there is
    no half-answer.
    """
    names = await _referenced_names(key, type_name, ctx)
    if names is None:
        return None, None

    # An unparseable name is still a reference: counted in `records_referenced`
    # and resolving to nothing. Dropping it hides what the caller is asking.
    parsed: list[uuid.UUID] = []
    for name in names:
        try:
            parsed.append(uuid.UUID(name))
        except (AttributeError, TypeError, ValueError):
            continue

    present: set[uuid.UUID] = set()
    async with degrade_on_db_error(ctx.db, what="cache_key_records") as probe:
        present = await JobRepository(ctx.db).existing_ids_for_tenant(
            parsed, ctx.principal.tenant_id
        )
    if probe.failed:
        # The half that gives the count meaning is missing; both go null.
        return None, None

    return len(names), sum(1 for job_id in parsed if job_id in present)


async def _referenced_names(
    key: str, type_name: str, ctx: ToolContext
) -> list[str] | None:
    """What this entry says it is a copy of, or `None` if unreadable.

    Two shapes: `cache:job:{tenant}:{job_id}` names one record in the key itself
    (the payload is never read), or a JSON array of scalars lists record ids.
    Anything else is `None` — guessing would be the fabrication this tool avoids.
    """
    from_key = job_segment(key)
    if from_key is not None:
        return [from_key]

    if type_name != "string":
        return None

    raw = await ctx.redis.get(key)
    if isinstance(raw, bytes | bytearray):
        raw = raw.decode(errors="replace")
    if not isinstance(raw, str):
        return None
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(loaded, list):
        return None
    if len(loaded) > _MAX_REFERENCES_CHECKED:
        logger.warning(
            "cache key references too many records to check",
            extra={"key": key, "entries": len(loaded)},
        )
        return None
    if not all(isinstance(item, str) for item in loaded):
        return None
    return [str(item) for item in loaded]


def _as_str(v: Any) -> str | None:
    """TYPE returns `str` under `decode_responses=True` and `bytes` otherwise."""
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
    "resolve_record_references",
]
