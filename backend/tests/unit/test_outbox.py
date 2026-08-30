"""Unit tests for the outbox repository and relay loop."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.repositories.outbox import OutboxRepository
from app.workers import dispatcher
from app.workers.schema_registry import SchemaValidationError

# ---------------------------------------------------------------------------
# OutboxRepository
# ---------------------------------------------------------------------------


async def test_repo_add_creates_row() -> None:
    session = AsyncMock()
    repo = OutboxRepository(session)
    # BaseRepository.create() calls session.add (sync) then session.flush/refresh
    session.add = MagicMock()
    tenant_id = uuid.uuid4()

    await repo.add(
        tenant_id=tenant_id, topic="job.submitted", key="user-1", payload={"x": 1}
    )

    session.add.assert_called_once()
    session.flush.assert_awaited()
    instance = session.add.call_args.args[0]
    assert instance.tenant_id == tenant_id
    assert instance.topic == "job.submitted"
    assert instance.key == "user-1"
    assert instance.payload == {"x": 1}


async def test_repo_mark_published_noop_on_empty_list() -> None:
    session = AsyncMock()
    repo = OutboxRepository(session)
    await repo.mark_published([])
    session.execute.assert_not_awaited()


async def test_repo_mark_published_executes_when_ids_given() -> None:
    session = AsyncMock()
    repo = OutboxRepository(session)
    await repo.mark_published([uuid.uuid4(), uuid.uuid4()])
    session.execute.assert_awaited_once()
    session.flush.assert_awaited_once()


async def test_repo_mark_failed_noop_on_empty_list() -> None:
    session = AsyncMock()
    repo = OutboxRepository(session)
    await repo.mark_failed([], "boom")
    session.execute.assert_not_awaited()


async def test_repo_mark_failed_sets_the_dead_letter_columns() -> None:
    """published_at lifts the row out of the fetch window; failed_at keeps it
    honest about never having been delivered; error_message says why."""
    session = AsyncMock()
    repo = OutboxRepository(session)
    await repo.mark_failed([uuid.uuid4()], "record too large")

    session.execute.assert_awaited_once()
    values = session.execute.await_args.args[0].compile().params
    assert values["published_at"] is not None
    assert values["failed_at"] is not None
    assert values["error_message"] == "record too large"


async def test_repo_mark_failed_truncates_a_giant_error() -> None:
    """An over-long error string would abort the marking transaction, which is
    the one transaction that must not fail — it is what ends the retry loop."""
    session = AsyncMock()
    repo = OutboxRepository(session)
    await repo.mark_failed([uuid.uuid4()], "x" * 10_000)

    values = session.execute.await_args.args[0].compile().params
    assert len(values["error_message"]) == 900


async def test_repo_fetch_unpublished_excludes_rows_past_the_cap() -> None:
    """The predicate, not just the marking branch, keeps a capped-out row out
    of the window — the marking write can be lost to a crash."""
    session = AsyncMock()
    # execute() is awaited, but its *result* is a sync object — an AsyncMock
    # would hand back a coroutine from .scalars().
    session.execute.return_value = MagicMock()
    repo = OutboxRepository(session)
    await repo.fetch_unpublished()

    sql = str(session.execute.await_args.args[0])
    assert "published_at IS NULL" in sql
    assert "attempts <" in sql


# ---------------------------------------------------------------------------
# _outbox_relay_loop
# ---------------------------------------------------------------------------


def _session_factory_with(events: list[MagicMock]) -> tuple[MagicMock, MagicMock]:
    """Build a session_factory and OutboxRepository mock that yields `events`
    on the first poll, then [] forever after — so the loop processes one batch."""
    repo = AsyncMock()
    repo.fetch_unpublished.side_effect = [events] + [[]] * 100
    repo.mark_published = AsyncMock()
    repo.increment_attempts = AsyncMock()

    begin_ctx = MagicMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_ctx)

    factory = MagicMock(return_value=session)
    return factory, repo


def _outbox_row(
    topic: str = "job.submitted",
    payload: dict | None = None,
    attempts: int = 0,
) -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.topic = topic
    row.key = "user-1"
    row.payload = payload or {"job_id": str(uuid.uuid4())}
    # A real int, not an auto-created MagicMock attribute: the relay does
    # arithmetic and a comparison on this to decide whether the row has
    # reached the attempt cap.
    row.attempts = attempts
    return row


async def test_relay_publishes_and_marks_each_event() -> None:
    events = [_outbox_row(), _outbox_row()]
    factory, repo = _session_factory_with(events)

    publish_mock = AsyncMock()

    with patch("app.workers.dispatcher.OutboxRepository", return_value=repo), \
         patch("app.workers.dispatcher.kafka_producer.publish_raw", new=publish_mock), \
         patch(
             "app.workers.dispatcher.asyncio.sleep",
             side_effect=[None, StopAsyncIteration],
         ):
        # First sleep returns (lets the empty-batch tick run), second raises to exit.
        with pytest.raises(StopAsyncIteration):
            await dispatcher._outbox_relay_loop(factory)

    assert publish_mock.await_count == 2
    repo.mark_published.assert_awaited()
    published_ids = repo.mark_published.await_args_list[0].args[0]
    assert set(published_ids) == {e.id for e in events}


async def test_relay_leaves_failed_rows_unpublished() -> None:
    """Rows whose publish raises must NOT be marked published — they retry next tick."""
    good = _outbox_row()
    bad = _outbox_row()
    factory, repo = _session_factory_with([good, bad])

    async def publish_side_effect(topic: str, key: str, payload: dict) -> None:
        if payload is bad.payload:
            raise RuntimeError("broker down")

    with patch("app.workers.dispatcher.OutboxRepository", return_value=repo), \
         patch(
             "app.workers.dispatcher.kafka_producer.publish_raw",
             side_effect=publish_side_effect,
         ), \
         patch(
             "app.workers.dispatcher.asyncio.sleep",
             side_effect=[None, StopAsyncIteration],
         ):
        with pytest.raises(StopAsyncIteration):
            await dispatcher._outbox_relay_loop(factory)

    published_ids = repo.mark_published.await_args_list[0].args[0]
    failed_ids = repo.increment_attempts.await_args_list[0].args[0]
    assert published_ids == [good.id]
    assert failed_ids == [bad.id]


async def test_relay_sleeps_when_outbox_is_empty() -> None:
    factory, repo = _session_factory_with([])

    with patch("app.workers.dispatcher.OutboxRepository", return_value=repo), \
         patch("app.workers.dispatcher.kafka_producer.publish_raw", new=AsyncMock()) as pub, \
         patch("app.workers.dispatcher.asyncio.sleep", side_effect=StopAsyncIteration):
        with pytest.raises(StopAsyncIteration):
            await dispatcher._outbox_relay_loop(factory)

    pub.assert_not_awaited()
    repo.mark_published.assert_not_awaited()


# ---------------------------------------------------------------------------
# Single-writer leader gate (E1-15 / ADR 0020)
#
# `worker_loop` runs in every API replica's lifespan, so two relays overlap
# on every rolling deploy. The gate is what stops the second one from
# republishing the whole backlog. SQLite has no advisory locks, so the real
# gate is a no-op here and leadership is injected instead — mutual exclusion
# itself is proved in tests/integration/test_outbox_relay_concurrency.py.
# ---------------------------------------------------------------------------


class _FakeGate:
    """Leader gate stand-in that records acquire/release per tick."""

    def __init__(self, is_leader: bool) -> None:
        self.is_leader = is_leader
        self.entered = 0
        self.exited = 0

    def __call__(self) -> "_FakeGate":
        return self

    async def __aenter__(self) -> bool:
        self.entered += 1
        return self.is_leader

    async def __aexit__(self, *exc_info: object) -> bool:
        self.exited += 1
        return False


async def test_relay_skips_the_whole_tick_when_not_leader() -> None:
    """Not the leader: no fetch, no publish, no mark. Fetching anyway would
    be harmless; publishing is what duplicates lifecycle events."""
    factory, repo = _session_factory_with([_outbox_row(), _outbox_row()])
    gate = _FakeGate(is_leader=False)
    publish_mock = AsyncMock()

    with patch("app.workers.dispatcher.OutboxRepository", return_value=repo), \
         patch("app.workers.dispatcher.kafka_producer.publish_raw", new=publish_mock), \
         patch(
             "app.workers.dispatcher.asyncio.sleep",
             side_effect=[None, StopAsyncIteration],
         ):
        with pytest.raises(StopAsyncIteration):
            await dispatcher._outbox_relay_loop(factory, leader_gate=gate)

    publish_mock.assert_not_awaited()
    repo.fetch_unpublished.assert_not_awaited()
    repo.mark_published.assert_not_awaited()
    repo.increment_attempts.assert_not_awaited()
    # Leadership is re-probed per tick, and the lock is given back each
    # time — a relay that grabbed it and kept it would never let a
    # surviving replica take over after a deploy.
    assert (gate.entered, gate.exited) == (2, 2)


async def test_relay_publishes_as_before_when_leader() -> None:
    events = [_outbox_row(), _outbox_row()]
    factory, repo = _session_factory_with(events)
    gate = _FakeGate(is_leader=True)
    publish_mock = AsyncMock()

    with patch("app.workers.dispatcher.OutboxRepository", return_value=repo), \
         patch("app.workers.dispatcher.kafka_producer.publish_raw", new=publish_mock), \
         patch(
             "app.workers.dispatcher.asyncio.sleep",
             side_effect=[None, StopAsyncIteration],
         ):
        with pytest.raises(StopAsyncIteration):
            await dispatcher._outbox_relay_loop(factory, leader_gate=gate)

    assert publish_mock.await_count == 2
    published_ids = repo.mark_published.await_args_list[0].args[0]
    assert set(published_ids) == {e.id for e in events}
    assert (gate.entered, gate.exited) == (2, 2)


async def test_relay_releases_leadership_when_a_tick_raises() -> None:
    """The lock must not survive a failed tick — otherwise one bad batch
    parks the relay on a process that has stopped making progress."""
    factory, repo = _session_factory_with([])
    repo.fetch_unpublished.side_effect = RuntimeError("db down")
    gate = _FakeGate(is_leader=True)

    with patch("app.workers.dispatcher.OutboxRepository", return_value=repo), \
         patch(
             "app.workers.dispatcher.asyncio.sleep",
             side_effect=[None, StopAsyncIteration],
         ):
        with pytest.raises(StopAsyncIteration):
            await dispatcher._outbox_relay_loop(factory, leader_gate=gate)

    assert gate.exited == gate.entered == 2


# ---------------------------------------------------------------------------
# JobService writes outbox row alongside the job row
# ---------------------------------------------------------------------------


async def test_job_service_create_writes_outbox() -> None:
    from app.models.enums import JobStatus, JobType
    from app.models.job import Job
    from app.services.job import JobService

    job_repo = AsyncMock()
    # JobService.create_job wraps the DB insert in `session.begin_nested()`.
    savepoint_ctx = MagicMock()
    savepoint_ctx.__aenter__ = AsyncMock(return_value=None)
    savepoint_ctx.__aexit__ = AsyncMock(return_value=False)
    job_repo.session = MagicMock()
    job_repo.session.begin_nested = MagicMock(return_value=savepoint_ctx)

    audit_repo = AsyncMock()
    outbox_repo = AsyncMock()
    redis = AsyncMock()

    job_repo.get_by_idempotency_key.return_value = None
    job = MagicMock(spec=Job)
    job.id = uuid.uuid4()
    job.trace_id = "trace-x"
    job.user_id = uuid.uuid4()
    job.status = JobStatus.PENDING
    job_repo.create.return_value = job

    svc = JobService(job_repo, audit_repo, outbox_repo, redis)
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    await svc.create_job(
        user_id=user_id, tenant_id=tenant_id, job_type=JobType.CSV_UPLOAD
    )

    outbox_repo.add.assert_awaited_once()
    kwargs = outbox_repo.add.await_args.kwargs
    assert kwargs["tenant_id"] == tenant_id
    assert kwargs["topic"] == "job.submitted"
    # Composite partition key — tenants spread across partitions, ordering
    # preserved per (tenant, user).
    assert kwargs["key"] == f"{tenant_id}:{user_id}"
    assert kwargs["payload"]["event"] == "job.submitted"
    assert kwargs["payload"]["tenant_id"] == str(tenant_id)
    assert kwargs["payload"]["job_id"] == str(job.id)


# ---------------------------------------------------------------------------
# Dead-lettering (WO-R2-05)
#
# ADR 0001 Decision item 3 has always specified this behaviour and the code
# has never had it: `increment_attempts` bumped a counter that nothing read,
# so a row that could never publish was retried every tick forever while
# holding one of the relay's fixed 100 fetch slots.
# ---------------------------------------------------------------------------


async def test_relay_dead_letters_a_schema_invalid_row_immediately() -> None:
    """A schema violation is deterministic: the same payload fails the same
    way on every future tick, so there is nothing to wait for."""
    bad = _outbox_row()
    factory, repo = _session_factory_with([bad])

    with patch("app.workers.dispatcher.OutboxRepository", return_value=repo), \
         patch(
             "app.workers.dispatcher.kafka_producer.publish_raw",
             side_effect=SchemaValidationError("job_id is not a uuid"),
         ), \
         patch(
             "app.workers.dispatcher.asyncio.sleep",
             side_effect=[None, StopAsyncIteration],
         ):
        with pytest.raises(StopAsyncIteration):
            await dispatcher._outbox_relay_loop(factory)

    repo.mark_failed.assert_awaited()
    failed_ids, error = repo.mark_failed.await_args_list[0].args
    assert failed_ids == [bad.id]
    assert "job_id is not a uuid" in error
    # Not retried, and not counted as a delivery.
    assert repo.increment_attempts.await_args_list[0].args[0] == []
    assert repo.mark_published.await_args_list[0].args[0] == []


async def test_relay_retries_a_transient_failure_below_the_cap() -> None:
    """A broker blip must not cost the row its place in the queue."""
    row = _outbox_row(attempts=3)
    factory, repo = _session_factory_with([row])

    with patch("app.workers.dispatcher.OutboxRepository", return_value=repo), \
         patch("app.workers.dispatcher.get_settings") as settings_mock, \
         patch(
             "app.workers.dispatcher.kafka_producer.publish_raw",
             side_effect=RuntimeError("broker down"),
         ), \
         patch(
             "app.workers.dispatcher.asyncio.sleep",
             side_effect=[None, StopAsyncIteration],
         ):
        settings_mock.return_value.outbox_max_attempts = 5
        with pytest.raises(StopAsyncIteration):
            await dispatcher._outbox_relay_loop(factory)

    assert repo.increment_attempts.await_args_list[0].args[0] == [row.id]
    repo.mark_failed.assert_not_awaited()


async def test_relay_dead_letters_a_row_that_reaches_the_attempt_cap() -> None:
    """The backstop for failures we cannot classify. One more failure takes
    this row to the cap, so it is abandoned instead of incremented."""
    row = _outbox_row(attempts=4)
    factory, repo = _session_factory_with([row])

    with patch("app.workers.dispatcher.OutboxRepository", return_value=repo), \
         patch("app.workers.dispatcher.get_settings") as settings_mock, \
         patch(
             "app.workers.dispatcher.kafka_producer.publish_raw",
             side_effect=RuntimeError("MessageSizeTooLargeError"),
         ), \
         patch(
             "app.workers.dispatcher.asyncio.sleep",
             side_effect=[None, StopAsyncIteration],
         ):
        settings_mock.return_value.outbox_max_attempts = 5
        with pytest.raises(StopAsyncIteration):
            await dispatcher._outbox_relay_loop(factory)

    repo.mark_failed.assert_awaited()
    failed_ids, error = repo.mark_failed.await_args_list[0].args
    assert failed_ids == [row.id]
    assert "abandoned after 5 attempts" in error
    assert "MessageSizeTooLargeError" in error
    assert repo.increment_attempts.await_args_list[0].args[0] == []


async def test_relay_dead_letters_one_row_without_disturbing_the_others() -> None:
    """Isolation was never the missing piece — the exit was. Both must hold at
    once: the poison row leaves, the healthy rows publish this same tick."""
    good_one = _outbox_row()
    poison = _outbox_row(attempts=99)
    good_two = _outbox_row()
    factory, repo = _session_factory_with([good_one, poison, good_two])

    async def publish_side_effect(topic: str, key: str, payload: dict) -> None:
        if payload is poison.payload:
            raise RuntimeError("record too large")

    with patch("app.workers.dispatcher.OutboxRepository", return_value=repo), \
         patch("app.workers.dispatcher.get_settings") as settings_mock, \
         patch(
             "app.workers.dispatcher.kafka_producer.publish_raw",
             side_effect=publish_side_effect,
         ), \
         patch(
             "app.workers.dispatcher.asyncio.sleep",
             side_effect=[None, StopAsyncIteration],
         ):
        settings_mock.return_value.outbox_max_attempts = 100
        with pytest.raises(StopAsyncIteration):
            await dispatcher._outbox_relay_loop(factory)

    assert repo.mark_published.await_args_list[0].args[0] == [good_one.id, good_two.id]
    assert repo.mark_failed.await_args_list[0].args[0] == [poison.id]
