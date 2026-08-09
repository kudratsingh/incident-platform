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

    # Promotion goes through the atomic WAITING->PENDING CAS (E1-04), not a
    # blind update_status write.
    job_repo.promote_waiting_to_pending.assert_awaited_once_with(child.id)
    outbox_repo.add.assert_awaited_once()


async def test_losing_promotion_cas_mints_no_duplicate_submitted() -> None:
    """E1-04: when a concurrent promoter (the resume sweep, or a redelivered
    job.completed) already flipped the child WAITING->PENDING, the losing
    promoter must ALSO skip the outbox add. CAS-ing the status alone is not
    enough — falling through to outbox_repo.add would still mint the
    duplicate job.submitted the old docstring shrugged off as 'redundant'."""
    factory, _ = _factory()
    resolver = DependencyResolver(factory)
    child = _waiting_child(uuid.uuid4())

    dep_repo = AsyncMock()
    dep_repo.children_of.return_value = [child.id]
    dep_repo.unmet_count.return_value = 0

    job_repo = AsyncMock()
    job_repo.get_by_id.return_value = child
    job_repo.promote_waiting_to_pending.return_value = False  # lost the CAS

    outbox_repo = AsyncMock()

    with patch("app.workers.dependency_resolver.JobDependencyRepository", return_value=dep_repo), \
         patch("app.workers.dependency_resolver.JobRepository", return_value=job_repo), \
         patch("app.workers.dependency_resolver.OutboxRepository", return_value=outbox_repo):
        await resolver.handle_message(
            topic="job.completed",
            key="u",
            value={"event": "job.completed", "job_id": str(uuid.uuid4())},
        )

    outbox_repo.add.assert_not_awaited()


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

    job_repo.promote_waiting_to_pending.assert_not_awaited()
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

    job_repo.promote_waiting_to_pending.assert_not_awaited()


async def _run_with_pause(redis: object) -> tuple[AsyncMock, AsyncMock, MagicMock]:
    """Drive one promotion attempt with a given Redis double attached."""
    factory, _ = _factory()
    resolver = DependencyResolver(factory, redis)
    child = _waiting_child(uuid.uuid4())

    dep_repo = AsyncMock()
    dep_repo.children_of.return_value = [child.id]
    dep_repo.unmet_count.return_value = 0
    dep_repo.parents.return_value = []  # seed has no ancestors

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
    return job_repo, outbox_repo, child


async def test_holds_child_while_dag_paused() -> None:
    """The regression this whole change exists for: before enforcement,
    `pause_dag` wrote `dag:paused:*` and the resolver promoted anyway."""
    redis = AsyncMock()
    redis.mget.return_value = ["paused"]  # seed's own flag is set

    job_repo, outbox_repo, _ = await _run_with_pause(redis)

    job_repo.promote_waiting_to_pending.assert_not_awaited()
    outbox_repo.add.assert_not_awaited()


async def test_promotes_child_when_no_pause_flag() -> None:
    redis = AsyncMock()
    redis.mget.return_value = [None]

    job_repo, outbox_repo, child = await _run_with_pause(redis)

    job_repo.promote_waiting_to_pending.assert_awaited_once_with(child.id)
    outbox_repo.add.assert_awaited_once()


async def test_promotes_when_pause_lookup_fails() -> None:
    """Redis is a performance dependency, never a correctness one — a
    lookup failure must not freeze every DAG in the system."""
    redis = AsyncMock()
    redis.mget.side_effect = RuntimeError("redis down")

    job_repo, outbox_repo, _ = await _run_with_pause(redis)

    job_repo.promote_waiting_to_pending.assert_awaited_once()
    outbox_repo.add.assert_awaited_once()


async def test_ancestor_pause_holds_descendant() -> None:
    """pause_dag(root) has to hold a grandchild, not just direct kids."""
    factory, _ = _factory()
    redis = AsyncMock()
    resolver = DependencyResolver(factory, redis)

    child = _waiting_child(uuid.uuid4())
    parent_id, root_id = uuid.uuid4(), uuid.uuid4()

    dep_repo = AsyncMock()
    dep_repo.children_of.return_value = [child.id]
    dep_repo.unmet_count.return_value = 0

    async def _parents(job_id: uuid.UUID) -> list[uuid.UUID]:
        return {child.id: [parent_id], parent_id: [root_id], root_id: []}[job_id]

    dep_repo.parents.side_effect = _parents
    # Walk order is [child, parent, root]; only the root carries a flag.
    redis.mget.return_value = [None, None, "paused"]

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

    job_repo.promote_waiting_to_pending.assert_not_awaited()


async def test_ignores_malformed_message() -> None:
    factory, _ = _factory()
    resolver = DependencyResolver(factory)

    # Missing job_id — must not crash.
    await resolver.handle_message(
        topic="job.completed", key=None, value={"event": "job.completed"}
    )
