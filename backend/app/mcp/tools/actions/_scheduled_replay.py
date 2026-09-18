"""Shared scheduled-replay step for `replay_dlq_by_ids` and `replay_dlq_by_category`.

R2-21: both branches used to arm the durable Redis entry before writing the
`job.replay_scheduled` audit row, outside any savepoint, so a rollback left a replay
that would fire with no audit trail. Here the audit row is INSERTed and flushed
first, the ZSET entry is armed at the `execute_at` it already records, and both sit
in a SAVEPOINT whose failure path also zrems the entry.
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

    Returns the epoch second the promote loop fires at; raises to the caller.
    """
    execute_at = time.time() + delay_seconds
    # Set BEFORE the zadd: a raising Redis call may still have reached the server,
    # so "we tried to arm" is what needs compensating.
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
                # The one residual armed-without-audit case; it needs a human.
                logger.error(
                    "scheduled replay left armed without an audit row",
                    extra={"job_id": str(job_id), "error": str(undo_exc)},
                )
        raise
    return execute_at
