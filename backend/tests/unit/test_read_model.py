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

    await projector.handle_message(
        topic="job.completed",
        key=user_id,
        value={
            "event": "job.completed",
            "job_id": job_id,
            "user_id": user_id,
            "job_type": "csv_upload",
        },
    )

    # Removed from every other status (idempotency); added to 'completed'.
    expected_remove_keys = {
        "jobs:status:running",
        "jobs:status:failed",
        "jobs:status:dead_letter",
        f"jobs:user:{user_id}:status:running",
        f"jobs:user:{user_id}:status:failed",
        f"jobs:user:{user_id}:status:dead_letter",
    }
    actual_remove_keys = {c.args[0] for c in redis.srem.await_args_list}
    assert actual_remove_keys == expected_remove_keys

    added = {c.args[0] for c in redis.sadd.await_args_list}
    assert added == {
        "jobs:status:completed",
        f"jobs:user:{user_id}:status:completed",
    }


async def test_failed_event_with_dlq_flag_lands_in_dead_letter() -> None:
    redis = AsyncMock()
    projector = ReadModelProjector(redis)
    job_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    await projector.handle_message(
        topic="job.dlq",
        key=user_id,
        value={
            "event": "job.failed",
            "job_id": job_id,
            "user_id": user_id,
            "dead_lettered": True,
        },
    )

    added = {c.args[0] for c in redis.sadd.await_args_list}
    assert added == {
        "jobs:status:dead_letter",
        f"jobs:user:{user_id}:status:dead_letter",
    }


async def test_failed_event_non_dlq_lands_in_failed() -> None:
    redis = AsyncMock()
    projector = ReadModelProjector(redis)
    user_id = str(uuid.uuid4())

    await projector.handle_message(
        topic="job.failed",
        key=user_id,
        value={
            "event": "job.failed",
            "job_id": str(uuid.uuid4()),
            "user_id": user_id,
            "dead_lettered": False,
        },
    )

    added = {c.args[0] for c in redis.sadd.await_args_list}
    assert "jobs:status:failed" in added


async def test_idempotent_under_redelivery() -> None:
    """Reprocessing the same message twice yields the same set membership —
    no double-counting because SADD on existing member is a no-op."""
    redis = AsyncMock()
    projector = ReadModelProjector(redis)
    msg = {
        "event": "job.completed",
        "job_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "job_type": "csv_upload",
    }

    await projector.handle_message(topic="job.completed", key="u", value=msg)
    first_sadd_calls = list(redis.sadd.await_args_list)
    await projector.handle_message(topic="job.completed", key="u", value=msg)
    second_sadd_calls = list(redis.sadd.await_args_list)
    # Same key arguments both times — and at the Redis layer, the second SADD
    # is a no-op since the id already exists. We assert the call shape here.
    assert len(second_sadd_calls) == 2 * len(first_sadd_calls)
    assert {c.args for c in first_sadd_calls} == {c.args for c in second_sadd_calls[2:]}


async def test_unknown_event_is_ignored() -> None:
    redis = AsyncMock()
    projector = ReadModelProjector(redis)
    await projector.handle_message(
        topic="job.submitted",
        key="u",
        value={
            "event": "job.submitted",  # not mapped — submission is just queued
            "job_id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
        },
    )
    redis.sadd.assert_not_called()
    redis.srem.assert_not_called()


async def test_read_global_stats_returns_cardinalities() -> None:
    redis = AsyncMock()
    redis.scard.side_effect = [3, 7, 1, 2]
    stats = await read_model.read_global_stats(redis)
    assert stats == {"running": 3, "completed": 7, "failed": 1, "dead_letter": 2}


async def test_read_user_stats_uses_user_keys() -> None:
    redis = AsyncMock()
    redis.scard.return_value = 0
    user_id = str(uuid.uuid4())
    await read_model.read_user_stats(redis, user_id)
    queried_keys = [c.args[0] for c in redis.scard.await_args_list]
    assert all(user_id in k for k in queried_keys)
