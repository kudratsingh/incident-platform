"""
Redis sorted sets for delayed DLQ replays (the `wait_and_replay` remediation), distinct from
`queue.py`'s `jobs:delayed`. Members are `{tenant_id}:{principal_id}:{job_id}` scored by fire-at.
Claim, don't pop (R2-21): `jobs:dlq_replay_delayed` holds armed replays, `jobs:dlq_replay_inflight`
ones a worker claimed. A *failed* replay is acked and dropped — policy is deliberately NOT to
re-enqueue it — where one whose worker *died* was never acked, so a later tick reclaims it.
"""

import time
import uuid

from redis.asyncio import Redis

SCHEDULED_KEY = "jobs:dlq_replay_delayed"
INFLIGHT_KEY = "jobs:dlq_replay_inflight"

# How long a claim is held before reclaim: longer than one replay, short enough that a crashed
# worker's replays are not stuck. 60s against a 0.5s POLL_INTERVAL.
CLAIM_TTL_SECONDS = 60.0

# One EVAL: lapsed in-flight claims first (the crash recovery, and unstarvable that way), then
# newly-due members up to the budget, then a fresh deadline on all of them. At most 1000 per call.
# Bounded (E1-12) like `queue._POP_READY_LUA`, because Lua's `unpack` ceiling wedges the set.
_CLAIM_READY_LUA = """
local budget = 1000
local claimed = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', ARGV[1], 'LIMIT', 0, budget)
local room = budget - #claimed
if room > 0 then
    local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, room)
    if #due > 0 then
        for i = 1, #due, 1000 do
            redis.call('ZREM', KEYS[1], unpack(due, i, math.min(i + 999, #due)))
        end
        for i = 1, #due do
            claimed[#claimed + 1] = due[i]
        end
    end
end
for i = 1, #claimed do
    redis.call('ZADD', KEYS[2], ARGV[2], claimed[i])
end
return claimed
"""


def _member(tenant_id: uuid.UUID, principal_id: uuid.UUID, job_id: uuid.UUID) -> str:
    return f"{tenant_id}:{principal_id}:{job_id}"


def _parse(member: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_str, principal_str, job_str = member.split(":", 2)
    return uuid.UUID(tenant_str), uuid.UUID(principal_str), uuid.UUID(job_str)


async def arm_replay(
    redis: Redis,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    job_id: uuid.UUID,
    execute_at: float,
) -> None:
    """Arm a replay at an explicit epoch second, so the `job.replay_scheduled` audit row can be
    written first and agree. Pair with `cancel_scheduled_replay` if that row does not survive."""
    await redis.zadd(
        SCHEDULED_KEY, {_member(tenant_id, principal_id, job_id): execute_at}
    )


async def schedule_replay(
    redis: Redis,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    job_id: uuid.UUID,
    delay_seconds: int,
) -> float:
    """Schedule a DLQ replay `delay_seconds` from now; returns that epoch second. For callers with
    nothing to write first, i.e. the promote loop's paused-DAG deferral."""
    execute_at = time.time() + delay_seconds
    await arm_replay(
        redis,
        tenant_id=tenant_id,
        principal_id=principal_id,
        job_id=job_id,
        execute_at=execute_at,
    )
    return execute_at


async def cancel_scheduled_replay(
    redis: Redis,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    job_id: uuid.UUID,
) -> None:
    """Disarm a scheduled replay — the compensating action for a
    `schedule_replay` whose surrounding transaction rolled back."""
    await redis.zrem(SCHEDULED_KEY, _member(tenant_id, principal_id, job_id))


async def claim_ready(
    redis: Redis,
) -> list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]:
    """Claim every due scheduled replay plus every lapsed in-flight claim, in one atomic EVAL.

    The caller owns each triple until `ack_replay`, so dying first lets the claim lapse for a later
    tick. At most 1000 per call; unparseable members are acked here.
    """
    now = time.time()
    raw = await redis.eval(
        _CLAIM_READY_LUA,
        2,
        SCHEDULED_KEY,
        INFLIGHT_KEY,
        str(now),
        str(now + CLAIM_TTL_SECONDS),
    )
    parsed: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = []
    for item in raw:
        # decode_responses is True in production; keep byte-safety for
        # tests that pass raw bytes back.
        member = item.decode() if isinstance(item, bytes) else str(item)
        try:
            parsed.append(_parse(member))
        except (ValueError, AttributeError):
            await redis.zrem(INFLIGHT_KEY, member)
            continue
    return parsed


async def ack_replay(
    redis: Redis,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    job_id: uuid.UUID,
) -> None:
    """Release a claim, on every outcome the promote loop can observe. Only a dead worker leaves
    one un-acked."""
    await redis.zrem(INFLIGHT_KEY, _member(tenant_id, principal_id, job_id))


async def scheduled_length(redis: Redis) -> int:
    return int(await redis.zcard(SCHEDULED_KEY))


async def inflight_length(redis: Redis) -> int:
    return int(await redis.zcard(INFLIGHT_KEY))
