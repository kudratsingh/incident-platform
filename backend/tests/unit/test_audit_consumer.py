"""Unit tests for AuditConsumer — sessions and AuditRepository fully mocked."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from app.workers.audit_consumer import AuditConsumer


def _factory_with_audit() -> tuple[MagicMock, AsyncMock]:
    audit_repo = AsyncMock()
    begin_ctx = MagicMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_ctx)

    factory = MagicMock(return_value=session)
    return factory, audit_repo


async def test_audit_consumer_writes_event_submitted_row() -> None:
    factory, audit_repo = _factory_with_audit()
    consumer = AuditConsumer(factory)
    job_id = uuid.uuid4()
    user_id = uuid.uuid4()

    tenant_id = uuid.uuid4()
    with patch("app.workers.audit_consumer.AuditRepository", return_value=audit_repo):
        await consumer.handle_message(
            topic="job.submitted",
            key=str(user_id),
            value={
                "event": "job.submitted",
                "tenant_id": str(tenant_id),
                "job_id": str(job_id),
                "user_id": str(user_id),
                "job_type": "csv_upload",
                "payload": {"file": "x.csv"},
                "priority": 0,
                "trace_id": "t1",
            },
        )

    audit_repo.log.assert_awaited_once()
    kwargs = audit_repo.log.await_args.kwargs
    args = audit_repo.log.await_args.args
    assert args[0] == "event.job.submitted"
    assert kwargs["tenant_id"] == tenant_id
    assert kwargs["user_id"] == user_id
    assert kwargs["job_id"] == job_id
    assert kwargs["extra_data"]["job_type"] == "csv_upload"
    # event/tenant_id/job_id/user_id stripped from extra_data
    assert "event" not in kwargs["extra_data"]
    assert "tenant_id" not in kwargs["extra_data"]
    assert "job_id" not in kwargs["extra_data"]


async def test_audit_consumer_splits_failed_vs_dead_letter() -> None:
    factory, audit_repo = _factory_with_audit()
    consumer = AuditConsumer(factory)

    with patch("app.workers.audit_consumer.AuditRepository", return_value=audit_repo):
        # Non-DLQ failure
        await consumer.handle_message(
            topic="job.failed",
            key="u",
            value={
                "event": "job.failed",
                "tenant_id": str(uuid.uuid4()),
                "job_id": str(uuid.uuid4()),
                "user_id": str(uuid.uuid4()),
                "dead_lettered": False,
                "error": "x",
            },
        )
        # DLQ failure
        await consumer.handle_message(
            topic="job.dlq",
            key="u",
            value={
                "event": "job.failed",
                "tenant_id": str(uuid.uuid4()),
                "job_id": str(uuid.uuid4()),
                "user_id": str(uuid.uuid4()),
                "dead_lettered": True,
                "error": "x",
            },
        )

    actions = [c.args[0] for c in audit_repo.log.await_args_list]
    assert actions == ["event.job.failed", "event.job.dead_letter"]


async def test_audit_consumer_skips_unmappable_event() -> None:
    factory, audit_repo = _factory_with_audit()
    consumer = AuditConsumer(factory)

    with patch("app.workers.audit_consumer.AuditRepository", return_value=audit_repo):
        await consumer.handle_message(
            topic="job.progress",
            key="u",
            value={"event": "job.progress", "job_id": str(uuid.uuid4())},
        )

    audit_repo.log.assert_not_awaited()


async def test_audit_consumer_skips_malformed_event() -> None:
    factory, audit_repo = _factory_with_audit()
    consumer = AuditConsumer(factory)

    with patch("app.workers.audit_consumer.AuditRepository", return_value=audit_repo):
        await consumer.handle_message(
            topic="job.submitted", key=None, value={"no_event_field": True}
        )

    audit_repo.log.assert_not_awaited()
