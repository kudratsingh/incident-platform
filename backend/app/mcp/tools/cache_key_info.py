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
    are tenant data. Existence / TTL / type / size, and the record check
    below, answer the operational question without exposing them.

Shape alone could not answer that question, which is what WO-R3-267
closes. An alert naming a stale key put the caller in front of
`exists / type / ttl_seconds / size` and nothing else, and those four
read the same for a current entry and a stale one: both are strings
with a TTL, differing only in byte count. `get_redis_health` adds a
Redis-WIDE miss ratio, which is not about this key. So the platform was
asserting staleness it could not show, and two live runs on identical
readings split on whether to act.

The evidence added is the one fact the platform can measure about any
entry in these namespaces without a second system: **every one of them
is a copy of job records Postgres holds, so the platform can ask
whether those records are still there.** `records_referenced` is how
many job records the entry is meant to name — the items in its stored
list, or the single job its key names — and `records_found` is how many
of those the database currently holds for the caller's tenant, looked
up at call time. A healthy hot-set entry names three jobs and finds
three; an entry written before the records it points at changed names
three and finds none. Neither number is ever invented: an entry whose
value names nothing lookupable reports `null` for both, which is not
zero.

Three candidates were considered and dropped, recorded here so the next
reader does not re-derive them:

  - **`written_at` / `age_seconds` from the cache writers.** No writer
    in these namespaces records a write time, and age is not staleness
    without something to compare it against — the healthy hot-set entry
    is written at boot and is hours old on a long-lived stack, while a
    freshly written stale one is seconds old. Age would have read
    backwards.
  - **`source_newer` from the cached row's own `updated_at`.** The
    natural version stamp for `cache:job:` entries, except that
    `JobResponse` — the shape `JobCache` serialises — carries
    `created_at` / `started_at` / `completed_at` and **no**
    `updated_at`. The platform cannot compare what it never cached, and
    adding the column to a REST response model to make a cache tool
    work is the tail wagging the dog.
  - **Redis `OBJECT IDLETIME` / `FREQ`.** Idle time is time since last
    *access*, and this tool's own `STRLEN` is an access — the first
    call would reset the signal the second call reads. It also measures
    reads, not writes, so a freshly written stale entry looks newer than
    a long-lived healthy one. Wrong direction, self-destroying.
  - Tenant-scoped (R2-54) — withholding the payload was not enough on its
    own. Existence, TTL and size of another tenant's cached job is an
    existence oracle over their jobs, so the tenant segment of the key
    now has to be the calling principal's. See
    `app/mcp/tools/_cache_scope.py`; the same check guards
    `invalidate_cache_key`.

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

# Most references the record check will resolve in one call. Beyond it the
# tool declines rather than checking a prefix and reporting the count as
# though it covered everything — a partial answer the caller could not tell
# from a whole one is the failure mode CLAUDE.md's "never promise
# completeness you cap" rule exists for, and here declining costs nothing:
# the largest list any writer of these namespaces produces is 100 entries.
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

    Public because the boot-time-consistency test in
    `tests/unit/test_eval_reset.py` asks this exact question of the
    hot-set entry the reset re-populates: the reading a caller gets
    after a reset has to be the healthy one, and asserting that through
    the real resolver is the only version of the assertion that cannot
    drift from the tool.

    Both halves are null together. Either the platform knows what
    records this entry refers to and can count them, or it says nothing
    — there is no half-answer, and a count that quietly covered part of
    the entry would be worse than none.
    """
    names = await _referenced_names(key, type_name, ctx)
    if names is None:
        return None, None

    # A name that is not a record id is still a reference the entry
    # makes — it is counted in `records_referenced` and simply resolves
    # to nothing. Dropping it instead would make an entry full of
    # unresolvable names indistinguishable from one that names no
    # records at all, which is the difference the caller is asking
    # about.
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
        # The count exists, but the half that gives it meaning does not.
        # Reporting the references alone would invite the reading
        # "referenced, therefore missing"; both go null.
        return None, None

    return len(names), sum(1 for job_id in parsed if job_id in present)


async def _referenced_names(
    key: str, type_name: str, ctx: ToolContext
) -> list[str] | None:
    """What this entry says it is a copy of, or `None` if unreadable.

    Two shapes, in this order:

      - The key names one record. `cache:job:{tenant}:{job_id}` is the
        platform's read-through per-job cache, so the key alone answers
        the question and the payload — tenant data — is never read.
      - The value lists them. A JSON array of scalars under any of the
        readable prefixes is a cached list of record ids; each item is
        one reference. Read internally to be counted, never returned.

    Anything else — a counter, a Redis collection type, a JSON object, a
    value that will not parse — is `None`: the platform does not know
    what the entry refers to, and guessing would be the fabrication this
    tool exists to avoid.
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
    "resolve_record_references",
]
