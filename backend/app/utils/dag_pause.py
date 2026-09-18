"""DAG pause flags — shared by the tool that sets them, the consumer that
enforces them, and the tool that reports them.

In `app/utils/` because `app.workers` must not import `app.mcp`. The flag is a
Redis key `dag:paused:<job_id>` with a TTL, and a WAITING child is held back if it
*or any ancestor* carries it. **Fail-open on Redis errors:** a failed lookup
promotes as if unpaused, since an outage must not freeze every DAG (ADR 0005).
"""

import uuid
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

# Bounded so a pathological graph cannot cause an unbounded crawl.
_MAX_ANCESTOR_NODES = 64


def pause_key_for(root_id: uuid.UUID | str) -> str:
    """Redis key `pause_dag` sets and the resolver probes."""
    return f"dag:paused:{root_id}"


async def collect_ancestors(
    dep_repo: Any, job_id: uuid.UUID, *, include_self: bool = True
) -> list[uuid.UUID]:
    """Breadth-first walk up the dependency edges from `job_id`.

    The job plus every transitive parent, deduped and capped at `_MAX_ANCESTOR_NODES`.
    """
    seen: set[uuid.UUID] = set()
    order: list[uuid.UUID] = []
    frontier = [job_id] if include_self else list(await dep_repo.parents(job_id))

    while frontier and len(order) < _MAX_ANCESTOR_NODES:
        node = frontier.pop(0)
        if node in seen:
            continue
        seen.add(node)
        order.append(node)
        frontier.extend(await dep_repo.parents(node))

    return order


async def find_blocking_pause(
    redis: Any, dep_repo: Any, job_id: uuid.UUID
) -> uuid.UUID | None:
    """The id of the job whose pause flag holds `job_id` back, else None (one `MGET`)."""
    try:
        nodes = await collect_ancestors(dep_repo, job_id)
        if not nodes:
            return None
        values = await redis.mget([pause_key_for(n) for n in nodes])
        for node, value in zip(nodes, values, strict=False):
            if value is not None:
                return node
        return None
    except Exception as exc:  # noqa: BLE001 — fail open, see module docstring
        logger.warning(
            "dag pause lookup failed; promoting as unpaused",
            extra={"job_id": str(job_id), "error": str(exc)},
        )
        return None


async def pause_state(redis: Any, job_id: uuid.UUID) -> tuple[bool, int | None]:
    """Direct pause flag for one job: `(paused, expires_in_seconds)`.

    Its own key only; TTL is None with no expiry.
    """
    try:
        key = pause_key_for(job_id)
        ttl = await redis.ttl(key)
        # redis-py: -2 = missing, -1 = present but no expiry.
        if ttl == -2:
            return False, None
        if ttl == -1:
            return True, None
        return True, int(ttl)
    except Exception as exc:  # noqa: BLE001 — fail open, see module docstring
        logger.warning(
            "dag pause state lookup failed; reporting unpaused",
            extra={"job_id": str(job_id), "error": str(exc)},
        )
        return False, None
