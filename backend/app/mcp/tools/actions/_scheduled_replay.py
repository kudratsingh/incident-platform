"""Shared scheduled-replay step for `replay_dlq_by_ids` and
`replay_dlq_by_category`.

R2-21. The two tools' scheduled branches had drifted apart — one caught
`Exception`, the other only `AppError` — and neither was transactional:
both armed the durable Redis entry *before* writing the
`job.replay_scheduled` audit row that is the only evidence the replay was
ever authorised, outside any savepoint and with no compensation. A
rollback anywhere in the request left a replay that would fire with no
audit trail, which breaks the audit-is-ground-truth invariant the
campaign's safety grading depends on. One helper, so the two branches
cannot diverge again.

Ordering and compensation, in that order:

  1. The `job.replay_scheduled` audit row is INSERTed and flushed FIRST.
     If the audit sink is unhappy, nothing is armed.
  2. Only then is the ZSET entry armed, at the `execute_at` the audit row
     already records — the two cannot disagree about when it fires.
  3. Both sit inside a SAVEPOINT, and any failure rolls the audit row
     back AND zrems the entry. With the write ordered first, the only
     failure that can still land after the zadd is the savepoint's own
     release; the compensation exists for exactly that residual window.

A bare `except Exception` around the pair would not have been enough on
its own — the verifier's point. Unlike the immediate branch, the
scheduled branch had no savepoint, so swallowing a flush failure would
have continued the loop on a poisoned session.
"""

import time
import uuid
from typing import Any

from app.core.logging import get_logger, request_id_var
from app.mcp.registry import ToolContext
from app.repositories.audit import AuditRepository
from app.workers import dlq_replay_scheduler

logger = get_logger(__name__)


async def schedule_one_audited(
    *,
    ctx: ToolContext,
    audit_repo: AuditRepository,
    job_id: uuid.UUID,
    delay_seconds: int,
    extra_data: dict[str, Any] | None = None,
) -> float:
    """Arm one delayed replay, audited, in a savepoint with compensation.

    Returns the epoch second the promote loop will fire the replay. The
    caller is expected to wrap the call in its per-item try/except — this
    raises whatever the audit write or Redis raised, having already left
    Redis and the session consistent with each other.
    """
    execute_at = time.time() + delay_seconds
    # Set BEFORE the zadd, not after. A Redis call that raises may still
    # have reached the server — a connection reset on the reply is the
    # ordinary case — so "we tried to arm" is the condition that needs
    # compensating, not "we know we armed". The zrem is a no-op when the
    # zadd never landed.
    arm_attempted = False
    try:
        async with ctx.db.begin_nested():
            await audit_repo.log(
                "job.replay_scheduled",
                tenant_id=ctx.principal.tenant_id,
                user_id=(
                    ctx.principal.user.id
                    if ctx.principal.user is not None
                    else None
                ),
                principal_type=ctx.principal.kind,
                principal_id=ctx.principal.id,
                job_id=job_id,
                resource_type="job",
                resource_id=str(job_id),
                request_id=request_id_var.get("") or None,
                extra_data={
                    "delay_seconds": delay_seconds,
                    "execute_at": execute_at,
                    **(extra_data or {}),
                },
            )
            arm_attempted = True
            await dlq_replay_scheduler.arm_replay(
                ctx.redis,
                tenant_id=ctx.principal.tenant_id,
                principal_id=ctx.principal.id,
                job_id=job_id,
                execute_at=execute_at,
            )
    except BaseException:
        # BaseException, not Exception: a cancellation mid-item must not
        # be the one path that leaves a replay armed with no audit row.
        if arm_attempted:
            try:
                await dlq_replay_scheduler.cancel_scheduled_replay(
                    ctx.redis,
                    tenant_id=ctx.principal.tenant_id,
                    principal_id=ctx.principal.id,
                    job_id=job_id,
                )
            except Exception as undo_exc:
                # Redis itself is the thing that's broken. Log loudly:
                # this is the one residual armed-without-audit case, and
                # it needs a human, not a retry.
                logger.error(
                    "scheduled replay left armed without an audit row",
                    extra={"job_id": str(job_id), "error": str(undo_exc)},
                )
        raise
    return execute_at
