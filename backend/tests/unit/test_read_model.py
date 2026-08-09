"""Unit tests for the CQRS read-model projector — Redis fully mocked."""

import uuid
from unittest.mock import AsyncMock

from app.workers import read_model
from app.workers.read_model import ReadModelProjector


async def test_completed_event_moves_job_into_completed_set() -> None:
    redis = AsyncMock()
    projector = ReadModelProjector(redis)
    job_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    tenant_id = str(uuid.uuid4())

    await projector.handle_message(
        topic="job.completed",
        key=f"{tenant_id}:{user_id}",
        value={
            "event": "job.completed",
            "tenant_id": tenant_id,
            "job_id": job_id,
            "user_id": user_id,
            "job_type": "csv_upload",
        },
    )

    expected_remove_keys = {
        f"jobs:tenant:{tenant_id}:status:running",
        f"jobs:tenant:{tenant_id}:status:failed",
        f"jobs:tenant:{tenant_id}:status:dead_letter",
        f"jobs:user:{user_id}:status:running",
        f"jobs:user:{user_id}:status:failed",
        f"jobs:user:{user_id}:status:dead_letter",
    }
    actual_remove_keys = {c.args[0] for c in redis.srem.await_args_list}
    assert actual_remove_keys == expected_remove_keys

    added = {c.args[0] for c in redis.sadd.await_args_list}
    assert added == {
        f"jobs:tenant:{tenant_id}:status:completed",
        f"jobs:user:{user_id}:status:completed",
    }


async def test_failed_event_with_dlq_flag_lands_in_dead_letter() -> None:
    redis = AsyncMock()
    projector = ReadModelProjector(redis)
    job_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    tenant_id = str(uuid.uuid4())

    await projector.handle_message(
        topic="job.dlq",
        key=f"{tenant_id}:{user_id}",
        value={
            "event": "job.failed",
            "tenant_id": tenant_id,
            "job_id": job_id,
            "user_id": user_id,
            "dead_lettered": True,
        },
    )

    added = {c.args[0] for c in redis.sadd.await_args_list}
    assert added == {
        f"jobs:tenant:{tenant_id}:status:dead_letter",
        f"jobs:user:{user_id}:status:dead_letter",
    }


async def test_failed_event_non_dlq_lands_in_failed() -> None:
    redis = AsyncMock()
    # The projector now consults the terminal sets before applying a
    # non-terminal transition; this job isn't in any of them.
    redis.sismember.return_value = 0
    projector = ReadModelProjector(redis)
    user_id = str(uuid.uuid4())
    tenant_id = str(uuid.uuid4())

    await projector.handle_message(
        topic="job.failed",
        key=f"{tenant_id}:{user_id}",
        value={
            "event": "job.failed",
            "tenant_id": tenant_id,
            "job_id": str(uuid.uuid4()),
            "user_id": user_id,
            "dead_lettered": False,
        },
    )

    added = {c.args[0] for c in redis.sadd.await_args_list}
    assert f"jobs:tenant:{tenant_id}:status:failed" in added


async def test_idempotent_under_redelivery() -> None:
    """Reprocessing the same message twice yields the same set membership —
    no double-counting because SADD on existing member is a no-op."""
    redis = AsyncMock()
    projector = ReadModelProjector(redis)
    msg = {
        "event": "job.completed",
        "tenant_id": str(uuid.uuid4()),
        "job_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "job_type": "csv_upload",
    }

    await projector.handle_message(topic="job.completed", key="u", value=msg)
    first_sadd_calls = list(redis.sadd.await_args_list)
    await projector.handle_message(topic="job.completed", key="u", value=msg)
    second_sadd_calls = list(redis.sadd.await_args_list)
    assert len(second_sadd_calls) == 2 * len(first_sadd_calls)
    assert {c.args for c in first_sadd_calls} == {c.args for c in second_sadd_calls[2:]}


async def test_progress_after_terminal_does_not_demote() -> None:
    """A job.progress consumed after that job's job.completed (cross-topic
    reordering or redelivery) must NOT drag the job back into the running
    set — at HEAD it did, permanently."""
    redis = AsyncMock()
    job_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    tenant_id = str(uuid.uuid4())
    completed_key = f"jobs:tenant:{tenant_id}:status:completed"

    def _member(key: str, member: str) -> int:
        return 1 if (key == completed_key and member == job_id) else 0

    redis.sismember.side_effect = _member

    projector = ReadModelProjector(redis)
    await projector.handle_message(
        topic="job.progress",
        key=f"{tenant_id}:{user_id}",
        value={
            "event": "job.progress",
            "tenant_id": tenant_id,
            "job_id": job_id,
            "user_id": user_id,
            "progress": 50,
        },
    )

    # The exact demotion the bug performed: sadd into ...:status:running and
    # srem out of the terminal sets. Neither may happen.
    running_adds = [
        c
        for c in redis.sadd.await_args_list
        if c.args[0] == f"jobs:tenant:{tenant_id}:status:running"
    ]
    assert running_adds == []
    redis.sadd.assert_not_awaited()
    redis.srem.assert_not_awaited()


async def test_terminal_event_still_applies_after_terminal() -> None:
    """Terminal→terminal is exempt from the guard: after an admin/agent DLQ
    replay, the job sits in the dead_letter set and the replay's
    job.completed MUST still project (a naive 'never touch terminal jobs'
    guard would freeze replayed jobs in dead_letter forever)."""
    redis = AsyncMock()
    job_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    tenant_id = str(uuid.uuid4())
    dead_letter_key = f"jobs:tenant:{tenant_id}:status:dead_letter"

    def _member(key: str, member: str) -> int:
        return 1 if (key == dead_letter_key and member == job_id) else 0

    redis.sismember.side_effect = _member

    projector = ReadModelProjector(redis)
    await projector.handle_message(
        topic="job.completed",
        key=f"{tenant_id}:{user_id}",
        value={
            "event": "job.completed",
            "tenant_id": tenant_id,
            "job_id": job_id,
            "user_id": user_id,
        },
    )

    # _move ran: the job lands in the completed sets (and leaves dead_letter).
    added = {c.args[0] for c in redis.sadd.await_args_list}
    assert added == {
        f"jobs:tenant:{tenant_id}:status:completed",
        f"jobs:user:{user_id}:status:completed",
    }
    removed = {c.args[0] for c in redis.srem.await_args_list}
    assert dead_letter_key in removed


async def test_unknown_event_is_ignored() -> None:
    redis = AsyncMock()
    projector = ReadModelProjector(redis)
    await projector.handle_message(
        topic="job.submitted",
        key="u",
        value={
            "event": "job.submitted",  # not mapped — submission is just queued
            "tenant_id": str(uuid.uuid4()),
            "job_id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
        },
    )
    redis.sadd.assert_not_called()
    redis.srem.assert_not_called()


async def test_missing_tenant_id_skips_projection() -> None:
    """Events without tenant_id (legacy or malformed) must not silently land
    in a global key — they used to before Phase 12. Now they're dropped."""
    redis = AsyncMock()
    projector = ReadModelProjector(redis)
    await projector.handle_message(
        topic="job.completed",
        key="u",
        value={
            "event": "job.completed",
            "job_id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
        },
    )
    redis.sadd.assert_not_called()


async def test_read_global_stats_returns_cardinalities_per_tenant() -> None:
    redis = AsyncMock()
    redis.scard.side_effect = [3, 7, 1, 2]
    tenant_id = str(uuid.uuid4())
    stats = await read_model.read_global_stats(redis, tenant_id)
    assert stats == {"running": 3, "completed": 7, "failed": 1, "dead_letter": 2}
    # All scard reads should be on tenant-scoped keys.
    queried = [c.args[0] for c in redis.scard.await_args_list]
    assert all(k.startswith(f"jobs:tenant:{tenant_id}:") for k in queried)


async def test_read_user_stats_uses_user_keys() -> None:
    redis = AsyncMock()
    redis.scard.return_value = 0
    user_id = str(uuid.uuid4())
    await read_model.read_user_stats(redis, user_id)
    queried_keys = [c.args[0] for c in redis.scard.await_args_list]
    assert all(user_id in k for k in queried_keys)
