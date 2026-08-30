"""Unit tests for the DLQ-replay scheduler ZSET.

Redis is fully mocked. Mirrors the pattern in `test_queue.py` —
same fixtures, same shape — since this is a sibling module with
identical Redis semantics.

R2-21 changed the reader half: the destructive `pop_ready` became a
`claim_ready` / `ack_replay` pair backed by a second sorted set
(`jobs:dlq_replay_inflight`). A claim that is never acked — because
the worker died between the claim and the replay — is reclaimed on a
later tick instead of being lost. The behavioural proof of that round
trip lives in `tests/api/test_mcp_dlq_categorization.py` against an
emulated ZSET; the tests here pin the call shapes.
"""

import time
import uuid
from unittest.mock import AsyncMock

from app.workers import dlq_replay_scheduler


def _mock_redis() -> AsyncMock:
    return AsyncMock()


async def test_schedule_replay_uses_future_timestamp() -> None:
    redis = _mock_redis()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    job_id = uuid.uuid4()

    before = time.time()
    execute_at = await dlq_replay_scheduler.schedule_replay(
        redis,
        tenant_id=tenant_id,
        principal_id=principal_id,
        job_id=job_id,
        delay_seconds=30,
    )
    after = time.time()

    redis.zadd.assert_awaited_once()
    # zadd(key, {member: score}) — inspect score
    call_key = redis.zadd.call_args[0][0]
    call_mapping = redis.zadd.call_args[0][1]
    assert call_key == dlq_replay_scheduler.SCHEDULED_KEY
    ((member, score),) = call_mapping.items()
    assert f"{tenant_id}:{principal_id}:{job_id}" == member
    assert before + 29 < score < after + 31
    assert execute_at == score


async def test_claim_ready_parses_members_and_stamps_a_claim_deadline() -> None:
    """The claim goes through one atomic EVAL over both keys: due members
    leave `jobs:dlq_replay_delayed` and land in
    `jobs:dlq_replay_inflight` scored with the claim deadline, so a
    worker that dies before acking does not take the replay with it."""
    redis = _mock_redis()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    job_a = uuid.uuid4()
    job_b = uuid.uuid4()
    redis.eval = AsyncMock(
        return_value=[
            f"{tenant_id}:{principal_id}:{job_a}".encode(),
            f"{tenant_id}:{principal_id}:{job_b}".encode(),
        ]
    )

    before = time.time()
    ready = await dlq_replay_scheduler.claim_ready(redis)
    after = time.time()

    assert ready == [
        (tenant_id, principal_id, job_a),
        (tenant_id, principal_id, job_b),
    ]
    script, numkeys, scheduled_key, inflight_key, now_arg, deadline_arg = (
        redis.eval.call_args[0]
    )
    assert script == dlq_replay_scheduler._CLAIM_READY_LUA
    assert numkeys == 2
    assert scheduled_key == dlq_replay_scheduler.SCHEDULED_KEY
    assert inflight_key == dlq_replay_scheduler.INFLIGHT_KEY
    assert before <= float(now_arg) <= after
    assert (
        float(deadline_arg) - float(now_arg)
        == dlq_replay_scheduler.CLAIM_TTL_SECONDS
    )
    # Single atomic call — no pipeline path.
    redis.pipeline.assert_not_called()


async def test_claim_ready_returns_empty_when_none_due() -> None:
    redis = _mock_redis()
    redis.eval = AsyncMock(return_value=[])
    assert await dlq_replay_scheduler.claim_ready(redis) == []


async def test_claim_ready_acks_malformed_members_instead_of_reclaiming() -> None:
    """A malformed member (from a bad manual write, say) can't be parsed
    into a triple. Under the old destructive pop it was simply skipped —
    the pop had already deleted it. A claim does NOT delete it, so it has
    to be acked explicitly or every tick would reclaim it forever."""
    redis = _mock_redis()
    redis.eval = AsyncMock(return_value=[b"not-a-triple"])

    assert await dlq_replay_scheduler.claim_ready(redis) == []

    redis.zrem.assert_awaited_once_with(
        dlq_replay_scheduler.INFLIGHT_KEY, "not-a-triple"
    )


async def test_ack_replay_removes_the_inflight_claim() -> None:
    redis = _mock_redis()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    job_id = uuid.uuid4()

    await dlq_replay_scheduler.ack_replay(
        redis, tenant_id=tenant_id, principal_id=principal_id, job_id=job_id
    )

    redis.zrem.assert_awaited_once_with(
        dlq_replay_scheduler.INFLIGHT_KEY,
        f"{tenant_id}:{principal_id}:{job_id}",
    )


async def test_cancel_scheduled_replay_disarms_the_scheduled_entry() -> None:
    """The tools' rollback compensation: an armed ZSET entry whose audit
    row did not survive must be removed, never left to fire unlogged."""
    redis = _mock_redis()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    job_id = uuid.uuid4()

    await dlq_replay_scheduler.cancel_scheduled_replay(
        redis, tenant_id=tenant_id, principal_id=principal_id, job_id=job_id
    )

    redis.zrem.assert_awaited_once_with(
        dlq_replay_scheduler.SCHEDULED_KEY,
        f"{tenant_id}:{principal_id}:{job_id}",
    )


async def test_scheduled_length_returns_zcard() -> None:
    redis = _mock_redis()
    redis.zcard.return_value = 4
    assert await dlq_replay_scheduler.scheduled_length(redis) == 4
    redis.zcard.assert_awaited_once_with(dlq_replay_scheduler.SCHEDULED_KEY)


async def test_inflight_length_returns_zcard() -> None:
    redis = _mock_redis()
    redis.zcard.return_value = 2
    assert await dlq_replay_scheduler.inflight_length(redis) == 2
    redis.zcard.assert_awaited_once_with(dlq_replay_scheduler.INFLIGHT_KEY)


def test_claim_lua_reclaims_expired_claims_and_stays_bounded() -> None:
    """Source-shape assertion, same honest scope note as
    `test_pop_ready_lua_is_bounded_and_chunk_zrems` in `test_queue.py`:
    `redis.eval` is mocked everywhere in this suite, so no unit test can
    observe the real interpreter. Three markers, all required:

      * `ZRANGEBYSCORE` on KEYS[2] with `'-inf', ARGV[1]` — expired claims
        are re-claimed, which IS the crash recovery. Without it a claim
        that is never acked is stranded in the in-flight set forever,
        which is strictly worse than the destructive pop it replaced.
      * `LIMIT` — bounded like the shared pop script, so the result can
        never reach Lua's `unpack` ceiling (E1-12).
      * chunked `unpack(due, i,` — keeps the ZREM safe if the LIMIT is
        ever raised.
    """
    lua = dlq_replay_scheduler._CLAIM_READY_LUA
    assert "ZRANGEBYSCORE', KEYS[2], '-inf', ARGV[1]" in lua
    assert "LIMIT" in lua
    assert "unpack(due, i," in lua


def test_destructive_pop_ready_is_gone() -> None:
    """Regression: `pop_ready` ZREM'd the whole due batch before any
    replay was attempted, so a worker crash mid-batch silently discarded
    operator/agent-scheduled replays. If it comes back it needs an
    in-flight claim to pair with — see the module docstring."""
    assert not hasattr(dlq_replay_scheduler, "pop_ready")
