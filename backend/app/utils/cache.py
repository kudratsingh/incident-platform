"""
Redis JSON cache helpers.

Provides a thin TTL-based cache layer on top of Redis.  Values are serialised
as JSON so any JSON-serialisable object can be stored.

Usage
-----
    from app.utils.cache import JobCache

    # Read-through pattern (keys are tenant-scoped)
    cached = await JobCache.get(redis, job_id, tenant_id)
    if cached is None:
        job = await repo.get_by_id(job_id)
        await JobCache.set(redis, job_id, tenant_id, job_data)

    # Invalidate on mutation, once the mutation has COMMITTED
    await JobCache.invalidate(redis, job_id, tenant_id)
"""

import json
import uuid
from typing import Any

from app.core.logging import get_logger
from redis.asyncio import Redis

logger = get_logger(__name__)

_JOB_TTL = 10  # seconds — short TTL; jobs change status frequently

# How long `invalidate` keeps the slot closed to writers. Must comfortably
# outlive an in-flight `GET /jobs/{id}`: the reader this is defending
# against is one that already read the pre-mutation row from Postgres and
# has not reached its `set` yet.
_NO_CACHE_TTL = 30  # seconds

# The tombstone `invalidate` parks in the slot. Deliberately not valid JSON
# at all, so a reader on an older deploy — one that does not know the
# sentinel — falls into `get`'s broad `except` and reads it as a miss.
# Degrading to a Postgres read is the right answer either way; the explicit
# check below only keeps it off the corrupt-payload warning path.
_INVALIDATED = "__invalidated__"


class JobCache:
    """Cache layer for individual job objects.

    Keys are tenant-scoped (``cache:job:{tenant_id}:{job_id}``) so a cache hit
    can never cross a tenant boundary: a caller from another tenant computes a
    different key, misses, and falls through to the tenant-scoped DB path
    (E2-01). The tenant therefore never needs to live in the cached payload.
    """

    @staticmethod
    def _key(job_id: uuid.UUID | str, tenant_id: uuid.UUID | str) -> str:
        # The ``cache:`` namespace is load-bearing, not cosmetic (E2-02):
        # it is the name docs/REDIS.md already catalogs, and it is what puts
        # this key under an allowlisted prefix of the MCP `invalidate_cache_key`
        # compensator — so an agent can force-refresh a stale job read without
        # widening that tool's allowlist (which would drift its inputSchema
        # against the pinned contract snapshot).
        #
        # Deploy-safe rename: the 10s TTL means keys orphaned under the old
        # ``job:{tenant}:{id}`` name expire within one TTL of rollout. No
        # migration, no backfill.
        return f"cache:job:{tenant_id}:{job_id}"

    @classmethod
    async def get(
        cls,
        redis: Redis,
        job_id: uuid.UUID | str,
        tenant_id: uuid.UUID | str,
    ) -> dict[str, Any] | None:
        """Return the cached job dict, or None on miss / Redis error /
        unusable payload.

        A cached value that is not a JSON object is treated as a miss
        rather than handed to the caller (R2-20). `GET /jobs/{id}` feeds
        this straight into `JobResponse.model_validate`, so anything
        else — a JSON array, a scalar, a truncated write — surfaced as
        an unhandled ValidationError and a 500 for as long as the entry
        lived. A cache is an optimisation: when its contents are
        unusable the honest behaviour is to miss and read Postgres, not
        to fail the request.
        """
        try:
            raw = await redis.get(cls._key(job_id, tenant_id))
            if raw is None:
                return None
            if raw in (_INVALIDATED, _INVALIDATED.encode()):
                # A recent committed mutation parked its tombstone here.
                # Reading Postgres is the whole point — say "miss" quietly
                # rather than routing this through the corrupt-payload
                # warning below, which it is not.
                return None
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                logger.warning(
                    "cache_get_unexpected_shape",
                    extra={
                        "job_id": str(job_id),
                        "tenant_id": str(tenant_id),
                        "payload_type": type(payload).__name__,
                    },
                )
                return None
            return payload
        except Exception:
            logger.warning(
                "cache_get_failed",
                extra={"job_id": str(job_id), "tenant_id": str(tenant_id)},
            )
            return None

    @classmethod
    async def set(
        cls,
        redis: Redis,
        job_id: uuid.UUID | str,
        tenant_id: uuid.UUID | str,
        data: dict[str, Any],
        ttl: int = _JOB_TTL,
    ) -> None:
        """Store a job dict in the cache with a TTL. Silently ignores errors.

        `nx=True` is what makes `invalidate` stick (R2-23). The caller
        reached here by missing the cache and reading Postgres, and that
        read may predate a replay that has since committed — so this write
        can be carrying a row that is already stale by the time it lands.
        Refusing to overwrite an occupied slot means it cannot bury the
        tombstone `invalidate` left there, and Redis decides that in the
        one `SET` rather than in a check-then-set window a racing reader
        can slip through.

        The cost is that a live entry is never refreshed mid-TTL: whoever
        wrote it wins for the remaining seconds. With a 10s TTL that is
        noise, and the loser's value was no fresher than the winner's.
        """
        try:
            await redis.set(
                cls._key(job_id, tenant_id), json.dumps(data), ex=ttl, nx=True
            )
        except Exception:
            logger.warning(
                "cache_set_failed",
                extra={"job_id": str(job_id), "tenant_id": str(tenant_id)},
            )

    @classmethod
    async def delete(
        cls,
        redis: Redis,
        job_id: uuid.UUID | str,
        tenant_id: uuid.UUID | str,
    ) -> None:
        """Drop a cached job outright. Silently ignores errors.

        Leaves the slot *empty*, so the next reader to finish a DB read
        repopulates it. Correct for a caller that just wants the entry
        gone; not sufficient after a status change — see `invalidate`.
        """
        try:
            await redis.delete(cls._key(job_id, tenant_id))
        except Exception:
            logger.warning(
                "cache_delete_failed",
                extra={"job_id": str(job_id), "tenant_id": str(tenant_id)},
            )

    @classmethod
    async def invalidate(
        cls,
        redis: Redis,
        job_id: uuid.UUID | str,
        tenant_id: uuid.UUID | str,
        ttl: int = _NO_CACHE_TTL,
    ) -> None:
        """Invalidate after a COMMITTED mutation. Silently ignores errors.

        `delete` on its own loses a race that a status change makes easy
        to hit (R2-23): a reader that missed and read the pre-mutation row
        from Postgres is still holding it, and its `set` can land after
        the delete — putting the old status back for a full TTL, on the
        one code path where the operator has just been told the job
        changed.

        So this parks a tombstone in the slot instead of emptying it. One
        `SET` both destroys the stale value and, against the `nx=True` in
        `set`, closes the slot to every writer for `ttl` seconds — long
        enough that any reader holding a pre-commit snapshot has given up
        trying. Readers miss and go to Postgres, which is the correct
        answer for exactly as long as we cannot tell a fresh write from a
        stale one.

        Call this *after* the transaction commits, not inside it. Before
        the commit the cached row is still what every other connection
        would read, so invalidating early only invites the same reader to
        refill the hole with the same value it already had.
        """
        try:
            await redis.set(cls._key(job_id, tenant_id), _INVALIDATED, ex=ttl)
        except Exception:
            logger.warning(
                "cache_invalidate_failed",
                extra={"job_id": str(job_id), "tenant_id": str(tenant_id)},
            )
