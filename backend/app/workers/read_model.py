"""
CQRS read-model projector.

Maintains denormalized job-status sets in Redis so admin queries don't have
to scan the jobs table. The write path is the normalized Postgres `jobs`
table; the read path is these projected views.

Why sets keyed by job_id rather than counters: at-least-once Kafka delivery
means counters can over-count under redelivery. A set keyed by job_id is
idempotent — re-adding the same id is a no-op. SCARD then gives the count.

Keys:
  jobs:tenant:{tenant_id}:status:{status} — per-tenant set of job_ids with that status
  jobs:user:{user_id}:status:{status}     — per-user set of job_ids with that status

Transitions are handled by removing the id from previous status sets and
adding to the new one; we infer "previous" by trying to remove from every
known status set (cheap O(N) where N = #statuses).

Tenant scoping: keys include `tenant_id` so a tenant admin's overview
cannot leak counts from sibling tenants. The pre-tenancy `jobs:status:*`
global key is gone; cross-tenant "platform" aggregation, if ever needed,
would sum across the per-tenant sets at read time.
"""

from typing import Any

from app.config import get_settings
from app.core.logging import get_logger
from app.workers.kafka_consumer import BaseKafkaConsumer
from redis.asyncio import Redis

logger = get_logger(__name__)

# Statuses we track in the projection. Strings rather than the enum so the
# projector doesn't need to import the SQLAlchemy enum module.
_TRACKED_STATUSES = ("running", "completed", "failed", "dead_letter")

# Map Kafka event → new status. job.submitted intentionally doesn't change a
# status set: the dispatcher creates the row in PENDING, and the next event
# (job.progress with status=running, or job.failed) moves it forward.
_EVENT_TO_STATUS: dict[str, str] = {
    "job.progress": "running",
    "job.completed": "completed",
}


def _tenant_key(tenant_id: str, status: str) -> str:
    return f"jobs:tenant:{tenant_id}:status:{status}"


def _user_key(user_id: str, status: str) -> str:
    return f"jobs:user:{user_id}:status:{status}"


async def _move(
    redis: Redis, tenant_id: str, user_id: str, job_id: str, new_status: str
) -> None:
    """Remove job_id from any other status set, then add to the new one."""
    # Remove from every other status (idempotent: SREM on non-member is a no-op).
    for st in _TRACKED_STATUSES:
        if st == new_status:
            continue
        await redis.srem(_tenant_key(tenant_id, st), job_id)  # type: ignore[misc,unused-ignore]
        await redis.srem(_user_key(user_id, st), job_id)  # type: ignore[misc,unused-ignore]
    await redis.sadd(_tenant_key(tenant_id, new_status), job_id)  # type: ignore[misc,unused-ignore]
    await redis.sadd(_user_key(user_id, new_status), job_id)  # type: ignore[misc,unused-ignore]


class ReadModelProjector(BaseKafkaConsumer):
    def __init__(self, redis: Redis) -> None:
        settings = get_settings()
        super().__init__(
            topics=[
                settings.kafka_topic_job_progress,
                settings.kafka_topic_job_completed,
                settings.kafka_topic_job_failed,
                settings.kafka_topic_job_dlq,
            ],
            group_id=settings.kafka_consumer_group_read_model,
        )
        self.redis = redis

    async def handle_message(
        self,
        topic: str,
        key: str | None,
        value: dict[str, Any],
        **_kafka_meta: Any,
    ) -> None:
        if not isinstance(value, dict):
            return

        job_id = value.get("job_id")
        user_id = value.get("user_id")
        tenant_id = value.get("tenant_id")
        event_name = value.get("event")
        if not (job_id and user_id and tenant_id and event_name):
            return

        # Determine the new status.
        if event_name == "job.failed":
            new_status = "dead_letter" if value.get("dead_lettered") is True else "failed"
        else:
            mapped = _EVENT_TO_STATUS.get(event_name)
            if mapped is None:
                return
            new_status = mapped

        await _move(
            self.redis, str(tenant_id), str(user_id), str(job_id), new_status
        )


async def read_global_stats(redis: Redis, tenant_id: str) -> dict[str, int]:
    """Status → count for one tenant (denormalized).

    Naming is historical — the function used to return cross-tenant
    counts, but post-Phase-12 it returns one tenant's view. Callers pass
    the effective tenant_id (their own or, for platform admins, the
    overridden one).
    """
    out: dict[str, int] = {}
    for st in _TRACKED_STATUSES:
        out[st] = int(await redis.scard(_tenant_key(tenant_id, st)))  # type: ignore[misc,unused-ignore]
    return out


async def read_user_stats(redis: Redis, user_id: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for st in _TRACKED_STATUSES:
        out[st] = int(await redis.scard(_user_key(user_id, st)))  # type: ignore[misc,unused-ignore]
    return out
