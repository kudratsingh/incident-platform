"""Unit tests for `app.utils.dag_pause` — the bits the resolver and
`get_dag_state` tests don't reach: the ancestor-walk bound and the
fail-open behaviour of the TTL read."""

import uuid
from unittest.mock import AsyncMock

from app.utils.dag_pause import (
    _MAX_ANCESTOR_NODES,
    collect_ancestors,
    pause_key_for,
    pause_state,
)


def test_pause_key_shape() -> None:
    job_id = uuid.uuid4()
    assert pause_key_for(job_id) == f"dag:paused:{job_id}"


async def test_collect_ancestors_walks_the_chain() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    dep_repo = AsyncMock()
    dep_repo.parents.side_effect = lambda j: {a: [b], b: [c], c: []}[j]

    assert await collect_ancestors(dep_repo, a) == [a, b, c]


async def test_collect_ancestors_is_bounded() -> None:
    """A pathological graph must not turn one promotion into an
    unbounded DB crawl — every node here claims a fresh parent."""
    dep_repo = AsyncMock()
    dep_repo.parents.side_effect = lambda _j: [uuid.uuid4()]

    walked = await collect_ancestors(dep_repo, uuid.uuid4())

    assert len(walked) == _MAX_ANCESTOR_NODES


async def test_collect_ancestors_survives_a_cycle() -> None:
    """Cycles are impossible by construction, but the `seen` set means
    a corrupt edge can't hang the resolver."""
    a, b = uuid.uuid4(), uuid.uuid4()
    dep_repo = AsyncMock()
    dep_repo.parents.side_effect = lambda j: {a: [b], b: [a]}[j]

    assert sorted(map(str, await collect_ancestors(dep_repo, a))) == sorted(
        map(str, [a, b])
    )


async def test_pause_state_fails_open_on_redis_error() -> None:
    redis = AsyncMock()
    redis.ttl.side_effect = RuntimeError("redis down")

    assert await pause_state(redis, uuid.uuid4()) == (False, None)


async def test_pause_state_handles_key_without_expiry() -> None:
    redis = AsyncMock()
    redis.ttl.return_value = -1  # present, no TTL

    assert await pause_state(redis, uuid.uuid4()) == (True, None)
