"""Unit tests for EventLogConsumer — DB fully mocked."""

import uuid
from unittest.mock import AsyncMock, MagicMock

from app.workers.event_log_consumer import EventLogConsumer
from sqlalchemy.exc import IntegrityError


def _session_factory() -> tuple[MagicMock, MagicMock]:
    """Build a session factory whose session.begin() is a working async ctx mgr."""
    begin_ctx = MagicMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_ctx)
    session.add = MagicMock()

    factory = MagicMock(return_value=session)
    return factory, session


async def test_appends_event_row_with_kafka_coords() -> None:
    factory, session = _session_factory()
    consumer = EventLogConsumer(factory)
    job_id = uuid.uuid4()

    await consumer.handle_message(
        topic="job.submitted",
        key="u",
        value={
            "event": "job.submitted",
            "job_id": str(job_id),
            "user_id": str(uuid.uuid4()),
            "job_type": "csv_upload",
        },
        partition=2,
        offset=42,
    )

    session.add.assert_called_once()
    row = session.add.call_args.args[0]
    assert row.event_name == "job.submitted"
    assert row.job_id == job_id
    assert row.kafka_topic == "job.submitted"
    assert row.kafka_partition == 2
    assert row.kafka_offset == 42


async def test_swallows_integrity_error_on_redelivery() -> None:
    """Kafka redelivery → unique constraint fires → consumer must not raise."""
    factory, session = _session_factory()
    # Make session.begin().__aexit__ raise IntegrityError on commit (the usual
    # surface for a unique-constraint violation).
    begin_ctx = session.begin.return_value
    begin_ctx.__aexit__ = AsyncMock(
        side_effect=IntegrityError("uq violated", params=None, orig=Exception())
    )

    consumer = EventLogConsumer(factory)
    # Must not raise.
    await consumer.handle_message(
        topic="job.submitted",
        key="u",
        value={
            "event": "job.submitted",
            "job_id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
        },
        partition=0,
        offset=0,
    )


async def test_skips_message_with_no_event_field() -> None:
    factory, session = _session_factory()
    consumer = EventLogConsumer(factory)

    await consumer.handle_message(
        topic="job.submitted",
        key="u",
        value={"no_event": True},
        partition=0,
        offset=0,
    )

    session.add.assert_not_called()


async def test_null_job_id_is_recorded() -> None:
    """Events without a parseable job_id still get logged (job_id stays NULL)."""
    factory, session = _session_factory()
    consumer = EventLogConsumer(factory)

    await consumer.handle_message(
        topic="job.progress",
        key="u",
        value={"event": "job.progress", "job_id": "not-a-uuid"},
        partition=0,
        offset=7,
    )

    session.add.assert_called_once()
    row = session.add.call_args.args[0]
    assert row.job_id is None
    assert row.kafka_offset == 7
