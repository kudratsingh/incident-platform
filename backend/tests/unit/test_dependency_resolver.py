"""Unit tests for DependencyResolver — repositories fully mocked."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.enums import JobStatus
from app.workers.dependency_resolver import DependencyResolver


def _factory() -> tuple[MagicMock, MagicMock]:
    begin_ctx = MagicMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_ctx)
    factory = MagicMock(return_value=session)
    return factory, session


def _waiting_child(user_id: uuid.UUID, type_: str = "csv_upload") -> MagicMock:
    child = MagicMock()
    child.id = uuid.uuid4()
    child.user_id = user_id
    child.status = JobStatus.WAITING
    child.type = type_
    child.payload = {"x": 1}
    child.priority = 0
    child.trace_id = "t"
    return child


async def test_promotes_child_when_all_parents_complete() -> None:
    factory, _ = _factory()
    resolver = DependencyResolver(factory)

    user_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    child = _waiting_child(user_id)

    dep_repo = AsyncMock()
    dep_repo.children_of.return_value = [child.id]
    dep_repo.unmet_count.return_value = 0

    job_repo = AsyncMock()
    job_repo.get_by_id.return_value = child

    outbox_repo = AsyncMock()

    with patch("app.workers.dependency_resolver.JobDependencyRepository", return_value=dep_repo), \
         patch("app.workers.dependency_resolver.JobRepository", return_value=job_repo), \
         patch("app.workers.dependency_resolver.OutboxRepository", return_value=outbox_repo):
        await resolver.handle_message(
            topic="job.completed",
            key=str(user_id),
            value={"event": "job.completed", "job_id": str(parent_id)},
        )

    job_repo.update_status.assert_awaited_once()
    args = job_repo.update_status.await_args.args
    assert args[0] == child.id
    assert args[1] == JobStatus.PENDING
    outbox_repo.add.assert_awaited_once()


async def test_skips_child_with_remaining_unmet_deps() -> None:
    factory, _ = _factory()
    resolver = DependencyResolver(factory)
    child = _waiting_child(uuid.uuid4())

    dep_repo = AsyncMock()
    dep_repo.children_of.return_value = [child.id]
    dep_repo.unmet_count.return_value = 1  # still has another parent

    job_repo = AsyncMock()
    job_repo.get_by_id.return_value = child

    outbox_repo = AsyncMock()

    with patch("app.workers.dependency_resolver.JobDependencyRepository", return_value=dep_repo), \
         patch("app.workers.dependency_resolver.JobRepository", return_value=job_repo), \
         patch("app.workers.dependency_resolver.OutboxRepository", return_value=outbox_repo):
        await resolver.handle_message(
            topic="job.completed",
            key="u",
            value={"event": "job.completed", "job_id": str(uuid.uuid4())},
        )

    job_repo.update_status.assert_not_awaited()
    outbox_repo.add.assert_not_awaited()


async def test_skips_child_that_is_not_waiting() -> None:
    """A redelivered job.completed might try to promote a child that already ran."""
    factory, _ = _factory()
    resolver = DependencyResolver(factory)
    child = _waiting_child(uuid.uuid4())
    child.status = JobStatus.COMPLETED  # already done somehow

    dep_repo = AsyncMock()
    dep_repo.children_of.return_value = [child.id]
    dep_repo.unmet_count.return_value = 0
    job_repo = AsyncMock()
    job_repo.get_by_id.return_value = child
    outbox_repo = AsyncMock()

    with patch("app.workers.dependency_resolver.JobDependencyRepository", return_value=dep_repo), \
         patch("app.workers.dependency_resolver.JobRepository", return_value=job_repo), \
         patch("app.workers.dependency_resolver.OutboxRepository", return_value=outbox_repo):
        await resolver.handle_message(
            topic="job.completed",
            key="u",
            value={"event": "job.completed", "job_id": str(uuid.uuid4())},
        )

    job_repo.update_status.assert_not_awaited()


async def test_ignores_malformed_message() -> None:
    factory, _ = _factory()
    resolver = DependencyResolver(factory)

    # Missing job_id — must not crash.
    await resolver.handle_message(
        topic="job.completed", key=None, value={"event": "job.completed"}
    )
