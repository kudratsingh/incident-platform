"""
Redis sorted sets for delayed DLQ replays.

Distinct from `app/workers/queue.py`'s `jobs:delayed` set. That one
holds jobs still in the retry cycle (fail-with-backoff); this one
holds explicit operator-initiated replays that should fire after a
wait window — the `wait_and_replay` remediation category.

Members are `{tenant_id}:{principal_id}:{job_id}` strings so the
promote loop knows *who* asked for the replay (audit + tenancy) and
which job to reset. Score is the epoch second the replay should
fire. Rescheduling the same triple updates the score in place — the
tool's idempotency wrapper already dedupes exact-repeat calls, so
this is only reached when the caller varies the delay.

Two keys, not one (R2-21):

  * `jobs:dlq_replay_delayed` — armed replays, scored by fire-at time.
  * `jobs:dlq_replay_inflight` — replays a worker has *claimed* and is
    executing, scored by the claim deadline.

The reader used to ZREM the whole due batch before attempting any
replay, so a worker crash or redeploy in that window silently
discarded operator/agent-scheduled replays with no record of the
loss. `jobs:delayed` survives the same window because
`_promote_delayed_once` re-pushes on failure; this path could not
borrow that trick, because its policy is deliberately NOT to
re-enqueue a replay that failed on its merits (the operator sees the
`job.replay_scheduled` row with no matching `job.replayed` row and
re-issues). A re-push would turn "logged and dropped" into "retried
forever".

So the claim splits the two cases the old shape conflated. A replay
that *failed* is acked and dropped, policy unchanged. A replay whose
worker *died* was never acked, so its claim lapses and the next tick
reclaims it. The cost is a replay that can fire twice if the worker
dies after `replay_job` commits but before the ack — bounded and
benign: the job is no longer `dead_letter`/`failed` by then, so the
second attempt is refused with a `JobError` and logged.
"""

import time
import uuid

from redis.asyncio import Redis

SCHEDULED_KEY = "jobs:dlq_replay_delayed"
INFLIGHT_KEY = "jobs:dlq_replay_inflight"

# How long a claim is held before another worker may reclaim it. Must
# comfortably exceed one replay (a single transaction: status update +
# audit row + outbox insert) so a slow-but-alive worker is never raced
# by a peer, and stay short enough that a crashed worker's replays are
# not stuck for long. 60s against a POLL_INTERVAL of 0.5s.
CLAIM_TTL_SECONDS = 60.0

# Claim, don't pop. One EVAL over both keys:
#
#   1. Re-claim anything in the in-flight set whose deadline has passed.
#      This IS the crash recovery — a worker that died between the claim
#      and the replay never acked, so its entries come back here. It runs
#      FIRST so recovered work is never starved by a large fresh batch.
#   2. Move newly-due members out of the scheduled set, up to the
#      remaining room in this call's budget.
#   3. Stamp every claimed member with a fresh deadline.
#
# Boundedness (E1-12), same discipline as `queue._POP_READY_LUA`: the
# ZRANGEBYSCOREs are LIMITed and the ZREM is chunked, so the result can
# never reach Lua's `unpack` ceiling (LUAI_MAXCSTACK, 8000 by default) —
# which, once hit, fails the EVAL on every subsequent tick and wedges the
# set permanently, since nothing is ever removed.
#
# Script contract:
#   KEYS[1] : scheduled sorted-set key
#   KEYS[2] : in-flight sorted-set key
#   ARGV[1] : now, epoch seconds (as string; Redis parses it)
#   ARGV[2] : claim deadline, epoch seconds
#   returns : member strings this call owns, AT MOST 1000. Empty list
#             when nothing is due. A backlog larger than the limit drains
#             across successive ticks.
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
    """Arm a replay to fire at an explicit epoch second.

    The primitive the audited tool path uses: it needs the `execute_at`
    up front so the `job.replay_scheduled` audit row can be written
    BEFORE the entry is armed, and so the row and the entry agree about
    when it fires. Pair with `cancel_scheduled_replay` if the audit row
    does not survive — an armed entry with no audit evidence is the one
    state the audit-is-ground-truth invariant does not allow.
    """
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
    """Schedule a DLQ replay to fire `delay_seconds` from now.

    Convenience wrapper over `arm_replay` for callers that have nothing
    to write first — the promote loop's paused-DAG deferral, which is
    re-arming an entry whose audit row was written when the operator
    scheduled it. Returns the epoch second the replay is scheduled for.
    """
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
    """Claim every scheduled replay whose `execute_at` has passed, plus
    every in-flight claim whose deadline lapsed (a worker died holding
    it). One atomic EVAL, so two concurrent readers can't take the same
    member.

    The caller owns each returned triple until it calls `ack_replay`. If
    the process dies first, the claim lapses and a later tick recovers
    it — which is the whole point of the pair. Bounded: at most 1000
    members per call, so callers must not assume they received every due
    member.

    Malformed members (from a bad manual write) can't be parsed into a
    triple, so nobody can ever ack them; they are acked here instead of
    being reclaimed on every tick forever.
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
    """Release a claim. Called once the replay has been fired, deferred
    onto the scheduled set again, or failed on its merits — every outcome
    the promote loop is able to observe. Only a dead worker leaves a
    claim un-acked, which is exactly the case reclaim exists for."""
    await redis.zrem(INFLIGHT_KEY, _member(tenant_id, principal_id, job_id))


async def scheduled_length(redis: Redis) -> int:
    return int(await redis.zcard(SCHEDULED_KEY))


async def inflight_length(redis: Redis) -> int:
    return int(await redis.zcard(INFLIGHT_KEY))
