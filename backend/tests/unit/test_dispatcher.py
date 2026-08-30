"""Unit tests for the worker dispatcher — DB and Redis fully mocked."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models.enums import JobStatus, JobType
from app.models.job import Job
from app.schemas import job_events
from app.utils.post_commit import register_post_commit
from app.workers import dispatcher
from redis.exceptions import ConnectionError as RedisConnectionError

# A well-formed W3C traceparent, so `extract_context` gets something real to
# parse when a test exercises the carrier-popping path.
_TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _make_job(**kwargs: object) -> MagicMock:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "type": JobType.BULK_API_SYNC,
        "status": JobStatus.PENDING,
        "payload": {},
        "retry_count": 0,
        "max_retries": 3,
        "trace_id": None,
    }
    defaults.update(kwargs)
    job = MagicMock(spec=Job)
    for k, v in defaults.items():
        setattr(job, k, v)
    return job


def _make_session() -> AsyncMock:
    """An AsyncSession stand-in usable as `async with factory() as session`
    and `async with session.begin()`."""
    # session.begin() must be a regular (sync) call returning an async context manager
    begin_ctx = MagicMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_ctx)
    # `execute` returns an empty result set rather than a bare AsyncMock so
    # the pause probe's `JobDependencyRepository.parents` walk resolves to
    # "no parents" instead of raising into `find_blocking_pause`'s fail-open
    # branch — the dispatch paths are unpaused in these tests by intent, not
    # by an accidental exception.
    empty_result = MagicMock()
    empty_result.all = MagicMock(return_value=[])
    session.execute = AsyncMock(return_value=empty_result)
    return session


def _make_session_factory(job: MagicMock) -> MagicMock:
    """Returns a session factory whose sessions yield a job_repo that returns `job`."""
    job_repo = AsyncMock()
    job_repo.get_by_id.return_value = job
    job_repo.update_status.return_value = job

    audit_repo = AsyncMock()
    audit_repo.log = AsyncMock()

    factory = MagicMock()
    factory.return_value = _make_session()

    return factory, job_repo, audit_repo


async def test_run_job_success_marks_completed() -> None:
    job = _make_job(type=JobType.BULK_API_SYNC)
    factory, job_repo, audit_repo = _make_session_factory(job)
    redis = AsyncMock()

    processor = AsyncMock(return_value={"ok": True})
    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=AsyncMock()),
         ), \
         patch.dict(dispatcher._PROCESSORS, {JobType.BULK_API_SYNC: processor}):
        await dispatcher._run_job(str(job.id), factory, redis)

    # The RUNNING transition goes through the atomic claim (E1-04), not a
    # blind update_status write.
    job_repo.claim_for_running.assert_awaited_once_with(job.id)
    calls = [c.args[1] for c in job_repo.update_status.call_args_list]
    assert JobStatus.COMPLETED in calls


async def test_run_job_duplicate_delivery_loses_claim_and_executes_nothing() -> None:
    """E1-04: two at-least-once deliveries of one job.submitted race into
    _run_job. The loser's atomic PENDING->RUNNING claim returns False and it
    must execute NOTHING: no processor call, no COMPLETED/DEAD_LETTER status
    writes, no outbox row. Before the fix, the check-then-act (SELECT +
    status check + blind UPDATE) let both deliveries run the processor."""
    job = _make_job(type=JobType.BULK_API_SYNC)
    factory, job_repo, audit_repo = _make_session_factory(job)
    job_repo.claim_for_running.return_value = False  # the other delivery won
    redis = AsyncMock()

    processor = AsyncMock(return_value={"ok": True})
    outbox_mock = AsyncMock()
    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=outbox_mock),
         ), \
         patch.dict(dispatcher._PROCESSORS, {JobType.BULK_API_SYNC: processor}):
        await dispatcher._run_job(str(job.id), factory, redis)

    processor.assert_not_awaited()
    job_repo.update_status.assert_not_awaited()
    outbox_mock.add.assert_not_awaited()


async def test_run_job_retries_on_failure() -> None:
    job = _make_job(type=JobType.BULK_API_SYNC, retry_count=0, max_retries=3)
    factory, job_repo, audit_repo = _make_session_factory(job)
    redis = AsyncMock()

    processor = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=AsyncMock()),
         ), \
         patch.dict(dispatcher._PROCESSORS, {JobType.BULK_API_SYNC: processor}), \
         patch("app.workers.dispatcher.queue.push_delayed", new=AsyncMock()) as mock_delay:
        await dispatcher._run_job(str(job.id), factory, redis)

    mock_delay.assert_awaited_once()
    calls = [c.args[1] for c in job_repo.update_status.call_args_list]
    assert JobStatus.PENDING in calls


async def test_retry_branch_survives_a_redis_outage_instead_of_dead_lettering() -> None:
    """The retry branch's `push_delayed` was the one call site of the four in
    this module with no try/except. The transaction before it has already
    committed status=PENDING and the "retrying" `job.failed` event, so a
    transient Redis error there escaped `_run_job`, hit `_run_and_release`'s
    safety net, and force-dead-lettered a job that still had 2 of 3 retries
    left — turning a Redis blip into permanent data loss.

    Redis is a performance dependency on this path, never a correctness one:
    the job must be left PENDING with its retry counted, for the
    stale-PENDING backstop to re-publish.
    """
    job = _make_job(type=JobType.BULK_API_SYNC, retry_count=0, max_retries=3)
    factory, job_repo, audit_repo = _make_session_factory(job)
    redis = AsyncMock()
    consumer = dispatcher.JobDispatcherConsumer(factory, redis)

    processor = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=AsyncMock()),
         ), \
         patch.dict(dispatcher._PROCESSORS, {JobType.BULK_API_SYNC: processor}), \
         patch(
             "app.workers.dispatcher.queue.push_delayed",
             new=AsyncMock(side_effect=RedisConnectionError("redis is down")),
         ):
        # Must not re-raise, and must not reach the force-dead-letter net.
        await consumer._run_and_release(str(job.id))

    calls = [c.args[1] for c in job_repo.update_status.call_args_list]
    assert JobStatus.PENDING in calls
    assert JobStatus.DEAD_LETTER not in calls

    pending_call = next(
        c for c in job_repo.update_status.call_args_list
        if c.args[1] == JobStatus.PENDING
    )
    assert pending_call.kwargs["extra"]["retry_count"] == 1


async def test_run_job_dead_letters_after_exhaustion() -> None:
    job = _make_job(
        type=JobType.BULK_API_SYNC,
        retry_count=2,
        max_retries=3,
        payload={"file": "x.csv", "__traceparent": {"traceparent": _TRACEPARENT}},
        trace_id="trace-exhausted",
    )
    factory, job_repo, audit_repo = _make_session_factory(job)
    redis = AsyncMock()

    outbox_mock = AsyncMock()
    processor = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=outbox_mock),
         ), \
         patch.dict(dispatcher._PROCESSORS, {JobType.BULK_API_SYNC: processor}), \
         patch("app.workers.dispatcher.queue.push_delayed", new=AsyncMock()) as mock_delay:
        await dispatcher._run_job(str(job.id), factory, redis)

    mock_delay.assert_not_awaited()
    calls = [c.args[1] for c in job_repo.update_status.call_args_list]
    assert JobStatus.DEAD_LETTER in calls

    # The `job.dlq` event is written by `update_status` in the same
    # transaction as the status, so this branch no longer builds a payload —
    # the shape assertions live at the producer, in `test_job_repository.py`.
    # What this branch still owns is the exhaustion wording, passed as the one
    # field a call site may colour.
    dead_letter_call = next(
        c for c in job_repo.update_status.call_args_list
        if c.args[1] == JobStatus.DEAD_LETTER
    )
    assert dead_letter_call.kwargs["event_message"] == (
        "Job exhausted after 3 attempts: boom"
    )
    # And it does NOT hand-write an outbox row of its own — a second `job.dlq`
    # for one death would double-trigger triage and the saga coordinator.
    outbox_mock.add.assert_not_awaited()


async def test_run_job_llm_policy_forces_dead_letter_before_exhaustion() -> None:
    """When the LLM-guided policy says dead_letter_now, the dispatcher must
    honor it even though there are deterministic retries remaining."""
    from app.services.retry_policy import RetryDecision

    job = _make_job(type=JobType.BULK_API_SYNC, retry_count=1, max_retries=5)
    factory, job_repo, audit_repo = _make_session_factory(job)
    redis = AsyncMock()

    processor = AsyncMock(side_effect=RuntimeError("401 Unauthorized"))
    fake_decision = RetryDecision(
        action="dead_letter_now",
        backoff_seconds=0,
        reasoning="Auth failure won't recover.",
    )
    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=AsyncMock()),
         ), \
         patch.dict(dispatcher._PROCESSORS, {JobType.BULK_API_SYNC: processor}), \
         patch(
             "app.workers.dispatcher.retry_policy.is_enabled", return_value=True
         ), \
         patch(
             "app.workers.dispatcher.retry_policy.decide_retry",
             new=AsyncMock(return_value=(fake_decision, {}, "claude-opus-4-7")),
         ), \
         patch("app.workers.dispatcher.queue.push_delayed", new=AsyncMock()) as mock_delay:
        await dispatcher._run_job(str(job.id), factory, redis)

    # Did NOT enqueue another retry — went straight to DLQ.
    mock_delay.assert_not_awaited()
    calls = [c.args[1] for c in job_repo.update_status.call_args_list]
    assert JobStatus.DEAD_LETTER in calls


def _dead_letter_extra(job_repo: AsyncMock) -> dict[str, object]:
    """The `extra` dict of the DEAD_LETTER update_status write on `job_repo`."""
    for call in job_repo.update_status.call_args_list:
        if call.args[1] == JobStatus.DEAD_LETTER:
            return dict(call.kwargs.get("extra") or {})
    raise AssertionError("no DEAD_LETTER update_status call was made")


async def test_run_job_llm_dead_letter_stamps_dead_lettered_by() -> None:
    """F2-16: the LLM-forced dead-letter is the ONLY mechanism that may
    claim attribution, and it must persist that on the jobs row — not just
    in the audit extra_data — because the DLQ admin table badges per row and
    cannot afford a per-row audit join."""
    from app.services.retry_policy import RetryDecision

    job = _make_job(type=JobType.BULK_API_SYNC, retry_count=1, max_retries=5)
    factory, job_repo, audit_repo = _make_session_factory(job)
    redis = AsyncMock()

    processor = AsyncMock(side_effect=RuntimeError("401 Unauthorized"))
    fake_decision = RetryDecision(
        action="dead_letter_now",
        backoff_seconds=0,
        reasoning="Auth failure won't recover.",
    )
    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=AsyncMock()),
         ), \
         patch.dict(dispatcher._PROCESSORS, {JobType.BULK_API_SYNC: processor}), \
         patch("app.workers.dispatcher.retry_policy.is_enabled", return_value=True), \
         patch(
             "app.workers.dispatcher.retry_policy.decide_retry",
             new=AsyncMock(return_value=(fake_decision, {}, "claude-opus-4-7")),
         ), \
         patch("app.workers.dispatcher.queue.push_delayed", new=AsyncMock()):
        await dispatcher._run_job(str(job.id), factory, redis)

    assert _dead_letter_extra(job_repo)["dead_lettered_by"] == "llm_retry_policy"


async def test_run_job_deterministic_exhaustion_does_not_stamp_dead_lettered_by() -> None:
    """Retries exhausting on their own is the DEFAULT mechanism — it leaves
    dead_lettered_by unset so the row renders unbadged."""
    job = _make_job(type=JobType.BULK_API_SYNC, retry_count=2, max_retries=3)
    factory, job_repo, audit_repo = _make_session_factory(job)
    redis = AsyncMock()

    processor = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=AsyncMock()),
         ), \
         patch.dict(dispatcher._PROCESSORS, {JobType.BULK_API_SYNC: processor}), \
         patch("app.workers.dispatcher.queue.push_delayed", new=AsyncMock()):
        await dispatcher._run_job(str(job.id), factory, redis)

    assert _dead_letter_extra(job_repo).get("dead_lettered_by") is None


async def test_run_job_compensation_dead_letter_is_not_attributed_to_the_llm() -> None:
    """The case F2-16 actually mislabeled.

    A saga compensation job with no registered processor dead-letters
    immediately at retry_count=0 — by design, and with LLM features off.
    Under the old `retry_count < max_retries` arithmetic that row was badged
    'LLM'. The persisted field must stay None here, and retry_count must
    still be below max_retries so the assertion genuinely covers the shape
    the arithmetic got wrong.
    """
    job = _make_job(type="csv_upload.compensate", retry_count=0, max_retries=3)
    factory, job_repo, audit_repo = _make_session_factory(job)
    redis = AsyncMock()

    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=AsyncMock()),
         ), \
         patch.dict(dispatcher._PROCESSORS, {}, clear=False):
        await dispatcher._run_job(str(job.id), factory, redis)

    extra = _dead_letter_extra(job_repo)
    assert extra["retry_count"] == 0 < job.max_retries
    assert extra.get("dead_lettered_by") is None


async def test_run_job_llm_policy_failure_falls_back_to_deterministic() -> None:
    """If the LLM call raises (timeout, network, schema mismatch), the
    deterministic exponential-backoff retry still happens — the worker
    can never block on the API being unhealthy."""
    job = _make_job(type=JobType.BULK_API_SYNC, retry_count=1, max_retries=5)
    factory, job_repo, audit_repo = _make_session_factory(job)
    redis = AsyncMock()

    processor = AsyncMock(side_effect=RuntimeError("HTTP 500"))
    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=AsyncMock()),
         ), \
         patch.dict(dispatcher._PROCESSORS, {JobType.BULK_API_SYNC: processor}), \
         patch(
             "app.workers.dispatcher.retry_policy.is_enabled", return_value=True
         ), \
         patch(
             "app.workers.dispatcher.retry_policy.decide_retry",
             new=AsyncMock(side_effect=RuntimeError("API down")),
         ), \
         patch("app.workers.dispatcher.queue.push_delayed", new=AsyncMock()) as mock_delay:
        await dispatcher._run_job(str(job.id), factory, redis)

    mock_delay.assert_awaited_once()
    calls = [c.args[1] for c in job_repo.update_status.call_args_list]
    assert JobStatus.PENDING in calls
    assert JobStatus.DEAD_LETTER not in calls


async def test_run_job_dead_letters_compensation_when_no_processor() -> None:
    """Saga compensation jobs have type=`{parent}.compensate`, which is NOT a
    valid JobType member. Before the fix, this raised ValueError inside
    _run_job outside any try/except, stranding the job in RUNNING and the
    saga in COMPENSATING. Now the dispatcher must:

      * NOT raise
      * mark the job DEAD_LETTER (not FAILED)
      * enqueue a `job.dlq` outbox row so the saga coordinator settles

    The dlq row asserted below is the settlement trigger: SagaCoordinator
    routes terminal events for `.compensate` jobs of a COMPENSATING saga
    into `_handle_compensation_settlement`, which lands the saga in FAILED
    (rollback dead-lettered) or COMPENSATED (rollback succeeded). Until
    WO-P4-01 that transition did not exist and the saga hung in
    COMPENSATING forever — see ADR 0017.
    """
    outbox_mock = AsyncMock()
    outbox_ctor = MagicMock(return_value=outbox_mock)

    # `type` is the compensate string, not a JobType member.
    job = _make_job(
        type="csv_upload.compensate",
        retry_count=0,
        max_retries=3,
        payload={"parent_job_id": "abc"},
        trace_id="trace-compensate",
    )
    factory, job_repo, audit_repo = _make_session_factory(job)
    redis = AsyncMock()

    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo), \
         patch("app.workers.dispatcher.OutboxRepository", new=outbox_ctor), \
         patch.dict(dispatcher._PROCESSORS, {}, clear=False):
        # Must NOT raise — the pre-fix code raised ValueError here.
        await dispatcher._run_job(str(job.id), factory, redis)

    # DEAD_LETTER, not FAILED.
    calls = [c.args[1] for c in job_repo.update_status.call_args_list]
    assert JobStatus.DEAD_LETTER in calls
    assert JobStatus.FAILED not in calls

    # The DLQ event rides on that DEAD_LETTER write (`update_status` emits it
    # in the same transaction), so the saga coordinator still learns the
    # compensation step died. This branch adds no message of its own — the
    # error IS the message — and writes no second outbox row.
    dead_letter_call = next(
        c for c in job_repo.update_status.call_args_list
        if c.args[1] == JobStatus.DEAD_LETTER
    )
    assert dead_letter_call.kwargs.get("event_message") is None
    assert dead_letter_call.kwargs["extra"]["error_message"] == (
        "No processor for type: csv_upload.compensate"
    )
    outbox_mock.add.assert_not_awaited()

    # And an audit row was written so the incident is visible in /audit/logs.
    audit_repo.log.assert_awaited()
    action = audit_repo.log.await_args.args[0]
    assert action == "job.dead_letter"


def test_payload_for_event_passes_small_payloads_through() -> None:
    payload = {"file": "x.csv", "rows": 10}
    assert job_events.payload_for_event(payload) == payload


def test_payload_for_event_truncates_oversized_payloads() -> None:
    """The DLQ event fans out to four consumer groups and is appended to
    `job_events`, so a user-controlled payload can't ride along unbounded."""
    payload = {"blob": "x" * (job_events.DLQ_PAYLOAD_MAX_BYTES * 2)}
    result = job_events.payload_for_event(payload)

    assert result is not None
    assert result["_truncated"] is True
    assert result["_original_bytes"] > job_events.DLQ_PAYLOAD_MAX_BYTES
    assert "blob" not in result


async def test_run_job_dead_letters_when_type_string_is_junk() -> None:
    """Same bug class: any job.type that isn't a JobType member (typo,
    schema drift, adversarial input) must dead-letter, not raise."""
    job = _make_job(type="totally_not_a_real_type")
    factory, job_repo, audit_repo = _make_session_factory(job)
    redis = AsyncMock()

    outbox_mock = AsyncMock()
    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo), \
         patch("app.workers.dispatcher.OutboxRepository", new=MagicMock(return_value=outbox_mock)):
        await dispatcher._run_job(str(job.id), factory, redis)

    calls = [c.args[1] for c in job_repo.update_status.call_args_list]
    assert JobStatus.DEAD_LETTER in calls


async def test_run_and_release_force_dead_letters_on_unhandled_exception() -> None:
    """Last-resort safety net: if _run_job escapes with an unhandled
    exception (a future bug), the safety net must mark the job DEAD_LETTER
    and log loudly rather than silently swallowing the error and stranding
    the job in RUNNING.

    The DEAD_LETTER write now carries the `job.dlq` outbox row with it —
    asserted against real rows in
    `test_terminal_event_single_write.py::test_force_dead_letter_writes_status_and_dlq_event_together`,
    because this suite mocks the repository that emits it. What this test
    still owns is that the net writes through that emitting path and does not
    hand-roll an outbox row beside it.
    """
    factory, job_repo, audit_repo = _make_session_factory(_make_job())
    redis = AsyncMock()
    consumer = dispatcher.JobDispatcherConsumer(factory, redis)
    consumer.session_factory = factory
    consumer.redis = redis

    job_id = str(uuid.uuid4())
    outbox_mock = AsyncMock()

    with patch(
        "app.workers.dispatcher._run_job",
        new=AsyncMock(side_effect=RuntimeError("boom past guards")),
    ), patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
       patch(
           "app.workers.dispatcher.OutboxRepository",
           new=MagicMock(return_value=outbox_mock),
       ), \
       patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo):
        # Must NOT re-raise.
        await consumer._run_and_release(job_id)

    # The safety net force-dead-lettered the job.
    calls = [c.args[1] for c in job_repo.update_status.call_args_list]
    assert JobStatus.DEAD_LETTER in calls
    outbox_mock.add.assert_not_awaited()


async def test_run_job_skips_unknown_job() -> None:
    begin_ctx = MagicMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_ctx)

    factory = MagicMock()
    factory.return_value = session

    job_repo = AsyncMock()
    job_repo.get_by_id.return_value = None

    redis = AsyncMock()

    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo):
        await dispatcher._run_job(str(uuid.uuid4()), factory, redis)

    job_repo.update_status.assert_not_awaited()


# ---------------------------------------------------------------------------
# JobDispatcherConsumer
# ---------------------------------------------------------------------------


async def test_dispatcher_consumer_spawns_run_job_for_valid_message() -> None:
    factory = MagicMock()
    redis = AsyncMock()
    consumer = dispatcher.JobDispatcherConsumer(factory, redis)
    job_id_str = str(uuid.uuid4())

    with patch("app.workers.dispatcher._run_job", new=AsyncMock()) as mock_run:
        await consumer.handle_message(
            topic="job.submitted",
            key="user-1",
            value={"job_id": job_id_str, "user_id": "user-1", "job_type": "csv_upload"},
        )
        # Let the spawned background task run to completion
        await asyncio.gather(*consumer.in_flight, return_exceptions=True)

    mock_run.assert_awaited_once_with(job_id_str, factory, redis)


async def test_dispatcher_consumer_skips_malformed_message() -> None:
    factory = MagicMock()
    redis = AsyncMock()
    consumer = dispatcher.JobDispatcherConsumer(factory, redis)

    with patch("app.workers.dispatcher._run_job", new=AsyncMock()) as mock_run:
        # Missing job_id — must return without raising and without dispatching.
        await consumer.handle_message(
            topic="job.submitted", key=None, value={"user_id": "x"}
        )

    mock_run.assert_not_awaited()
    assert not consumer.in_flight


async def test_dispatcher_consumer_semaphore_releases_on_run_failure() -> None:
    """If _run_job raises, the semaphore must still release so we don't deadlock."""
    factory = MagicMock()
    redis = AsyncMock()
    consumer = dispatcher.JobDispatcherConsumer(factory, redis, max_concurrent=1)

    with patch(
        "app.workers.dispatcher._run_job",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        await consumer.handle_message(
            topic="job.submitted", key="u", value={"job_id": str(uuid.uuid4())}
        )
        await asyncio.gather(*consumer.in_flight, return_exceptions=True)

    # Semaphore should be back at 1 — i.e. a fresh acquire returns immediately.
    assert consumer.semaphore.locked() is False


# ---------------------------------------------------------------------------
# Supervisor lifecycle (ADR 0009): boot-start retry + fail-closed kill window
# ---------------------------------------------------------------------------


class _FakeSupervisedConsumer:
    """BaseKafkaConsumer stand-in with scripted start()/run() outcomes.

    Exposes only the surface `_supervise_consumer` touches: group_id,
    start/stop/run, is_running, chaos_killed.
    """

    def __init__(
        self,
        *,
        start_failures: int = 0,
        killed_runs: int = 0,
        running: bool = False,
    ) -> None:
        self.group_id = "fake-group"
        self.start_calls = 0
        self.stop_calls = 0
        self.run_calls = 0
        self._start_failures = start_failures
        self._killed_runs = killed_runs
        self.is_running = running
        self.chaos_killed = False

    async def start(self) -> None:
        self.start_calls += 1
        if self.start_calls <= self._start_failures:
            raise ConnectionError("kafka bootstrap unreachable")
        self.is_running = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self.is_running = False

    async def run(self) -> None:
        self.run_calls += 1
        # run() always returns "stopped" shaped — is_running False is what
        # stop() would have left behind. chaos_killed is scripted so the
        # supervisor takes the kill-window branch only on the first N runs.
        self.is_running = False
        self.chaos_killed = self.run_calls <= self._killed_runs


class _FlakyRedis:
    """Redis client whose GET raises for the first `failures` calls, then
    reports the key as absent."""

    def __init__(self, failures: int) -> None:
        self.get_calls = 0
        self._failures = failures

    async def get(self, key: str) -> None:
        self.get_calls += 1
        if self.get_calls <= self._failures:
            raise ConnectionError("redis saturated")
        return None


async def test_supervise_consumer_retries_failed_boot_start() -> None:
    """A consumer whose start() fails at boot is retried, not dropped.

    Supervision owns start(): worker_loop hands over an unstarted consumer,
    so a transient Kafka/DNS error at boot must go through the same
    stop()+start() backoff as a crash restart.
    """
    consumer = _FakeSupervisedConsumer(start_failures=1)

    with patch("asyncio.sleep", new=AsyncMock()):
        await dispatcher._supervise_consumer(consumer)

    assert consumer.start_calls >= 2, "boot start failure was not retried"
    assert consumer.run_calls >= 1, "run() never entered after the retried start"


async def test_supervise_consumer_holds_consumer_down_on_kill_key_lookup_error() -> None:
    """A Redis error during the chaos kill window must not read as 'cleared'.

    Fail-open would restart the consumer after the very first failed lookup,
    resurrecting it mid-kill-window and voiding the chaos scenario.
    """
    consumer = _FakeSupervisedConsumer(killed_runs=1, running=True)
    flaky = _FlakyRedis(failures=2)
    lookups_at_restart: list[int] = []

    async def _record_restart(_consumer: object) -> None:
        lookups_at_restart.append(flaky.get_calls)

    with (
        patch("app.core.redis.get_redis_client", return_value=flaky),
        patch(
            "app.workers.dispatcher._restart_consumer",
            new=AsyncMock(side_effect=_record_restart),
        ) as mock_restart,
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        await dispatcher._supervise_consumer(consumer)

    assert mock_restart.await_count == 1
    # Restart only after the 3rd lookup — the first two raised, and an
    # unknown kill state must hold the consumer down.
    assert lookups_at_restart == [3]


async def test_supervise_consumer_does_not_resurrect_on_orderly_stop() -> None:
    """run() returning with is_running False and no chaos kill ends supervision.

    Guards the boot-start guard's placement: inside the while loop it would
    restart every consumer during shutdown.
    """
    consumer = _FakeSupervisedConsumer(running=True)

    with patch("asyncio.sleep", new=AsyncMock()):
        await dispatcher._supervise_consumer(consumer)

    assert consumer.run_calls == 1
    assert consumer.start_calls == 0, "orderly stop resurrected the consumer"


# ---------------------------------------------------------------------------
# Delayed-retry promotion (_promote_delayed_once)
# ---------------------------------------------------------------------------


async def test_promote_delayed_once_isolates_failures_and_requeues() -> None:
    """E1-03: the batch popped from `jobs:delayed` is already ZREM'd, so
    anything the promotion pass drops is gone from Redis forever and the job
    sits in PENDING with nothing left to publish it.

    Pre-fix the whole `for` loop lived inside one `try`, so the FIRST DB
    error aborted the pass: three ids popped, zero promoted, all three lost.
    Post-fix each item owns its try/except (the discipline already applied to
    `_promote_dlq_replay_loop`) and — unlike that loop, which deliberately
    does not re-enqueue because the operator can see the miss in the audit
    trail — the failed delayed retry is pushed back onto `jobs:delayed`.
    """
    ids = [uuid.uuid4() for _ in range(3)]
    jobs = [_make_job(id=job_id, tenant_id=uuid.uuid4()) for job_id in ids]

    job_repo = AsyncMock()
    # The first id never reaches the repo — its session_factory() call raises.
    job_repo.get_by_id.side_effect = [jobs[1], jobs[2]]
    outbox = AsyncMock()
    redis = AsyncMock()

    factory = MagicMock(
        side_effect=[RuntimeError("db connection lost"), _make_session(), _make_session()]
    )

    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=outbox),
         ), \
         patch(
             "app.workers.dispatcher.queue.pop_ready_delayed",
             new=AsyncMock(return_value=[str(job_id) for job_id in ids]),
         ), \
         patch(
             "app.workers.dispatcher.queue.push_delayed", new=AsyncMock()
         ) as mock_push:
        await dispatcher._promote_delayed_once(factory, redis)

    # The two healthy items still got promoted — one bad item no longer
    # strands the rest of the batch.
    assert outbox.add.await_count == 2
    promoted = {c.kwargs["payload"]["job_id"] for c in outbox.add.await_args_list}
    assert promoted == {str(ids[1]), str(ids[2])}

    # ...and the bad item went back onto the delayed set instead of vanishing.
    mock_push.assert_awaited_once_with(
        redis, str(ids[0]), dispatcher._PROMOTE_RETRY_DELAY_SECONDS
    )


async def test_promote_delayed_once_survives_a_failing_re_push() -> None:
    """If the re-enqueue itself fails (Redis blip) the pass must keep going —
    the job is logged as possibly stranded and left to the backstop sweep."""
    job_id = uuid.uuid4()
    job_repo = AsyncMock()
    outbox = AsyncMock()
    redis = AsyncMock()
    factory = MagicMock(side_effect=[RuntimeError("db connection lost")])

    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=outbox),
         ), \
         patch(
             "app.workers.dispatcher.queue.pop_ready_delayed",
             new=AsyncMock(return_value=[str(job_id)]),
         ), \
         patch(
             "app.workers.dispatcher.queue.push_delayed",
             new=AsyncMock(side_effect=RuntimeError("redis down")),
         ):
        # Must NOT raise out of the pass.
        await dispatcher._promote_delayed_once(factory, redis)

    outbox.add.assert_not_awaited()


# ---------------------------------------------------------------------------
# Stale-PENDING backstop sweep (_requeue_stale_pending_once)
# ---------------------------------------------------------------------------


def _stale_pending_factory(job: MagicMock) -> MagicMock:
    """Session factory whose single `execute` returns `job` as the only row."""
    session = _make_session()
    result = MagicMock()
    result.scalars = MagicMock(return_value=iter([job]))
    session.execute = AsyncMock(return_value=result)
    factory = MagicMock(return_value=session)
    return factory


async def test_requeue_stale_pending_once_republishes_orphaned_job() -> None:
    """E1-03 crash windows: a worker that dies between the Lua pop and the
    outbox commit (or between the retry tx commit and `push_delayed`) leaves a
    job PENDING with no Redis timer and no Kafka message. Nothing but this
    sweep can recover it.

    Duplicate-safe on top of WO-P4-03: a second `job.submitted` loses the
    atomic PENDING->RUNNING claim (`JobRepository.claim_for_running`) and
    executes nothing.
    """
    job = _make_job(status=JobStatus.PENDING, tenant_id=uuid.uuid4())
    factory = _stale_pending_factory(job)
    outbox = AsyncMock()
    redis = AsyncMock()
    redis.zscore = AsyncMock(return_value=None)  # no live backoff timer

    with patch(
        "app.workers.dispatcher.OutboxRepository",
        new=MagicMock(return_value=outbox),
    ):
        await dispatcher._requeue_stale_pending_once(factory, redis)

    redis.zscore.assert_awaited_once_with(
        dispatcher.queue.DELAYED_KEY, str(job.id)
    )
    outbox.add.assert_awaited_once()
    payload = outbox.add.await_args.kwargs["payload"]
    assert payload["event"] == "job.submitted"
    assert payload["job_id"] == str(job.id)


async def test_requeue_stale_pending_once_respects_a_live_backoff() -> None:
    """The LLM retry policy can legitimately park a job in PENDING for
    minutes. A ZSCORE hit on `jobs:delayed` means the promotion loop still
    owns this job — re-publishing would run it before its backoff elapsed."""
    job = _make_job(status=JobStatus.PENDING, tenant_id=uuid.uuid4())
    factory = _stale_pending_factory(job)
    outbox = AsyncMock()
    redis = AsyncMock()
    redis.zscore = AsyncMock(return_value=1_900_000_000.0)  # still waiting

    with patch(
        "app.workers.dispatcher.OutboxRepository",
        new=MagicMock(return_value=outbox),
    ):
        await dispatcher._requeue_stale_pending_once(factory, redis)

    outbox.add.assert_not_awaited()


# ---------------------------------------------------------------------------
# DAG pause enforcement on the dispatch paths (E1-08)
# ---------------------------------------------------------------------------


async def test_promote_delayed_once_holds_a_job_in_a_paused_dag() -> None:
    """E1-08: the retry cycle was a pause bypass.

    FAILED -> PENDING -> `jobs:delayed` -> this pass republished the job
    with no pause probe at all, so a failing step inside a paused chain
    kept re-executing every backoff while `get_dag_state` reported the
    DAG paused. ADR 0011's enforcement covered the resolver and the
    resume sweep only — a retry is a *new* dispatch, not the "work in
    flight" the pause deliberately does not recall.

    The held job must be re-pushed onto `jobs:delayed`, never dropped:
    `pop_ready_delayed` already ZREM'd it, so the "job not found"
    `continue` path would lose the retry permanently.
    """
    job_id = uuid.uuid4()
    job = _make_job(id=job_id, tenant_id=uuid.uuid4())

    job_repo = AsyncMock()
    job_repo.get_by_id.return_value = job
    outbox = AsyncMock()
    redis = AsyncMock()
    factory = MagicMock(return_value=_make_session())
    paused_by = uuid.uuid4()

    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=outbox),
         ), \
         patch(
             "app.workers.dispatcher.queue.pop_ready_delayed",
             new=AsyncMock(return_value=[str(job_id)]),
         ), \
         patch(
             "app.workers.dispatcher.find_blocking_pause",
             new=AsyncMock(return_value=paused_by),
         ), \
         patch(
             "app.workers.dispatcher.queue.push_delayed", new=AsyncMock()
         ) as mock_push:
        await dispatcher._promote_delayed_once(factory, redis)

    outbox.add.assert_not_awaited()
    mock_push.assert_awaited_once_with(
        redis, str(job_id), dispatcher._PAUSE_RECHECK_SECONDS
    )


async def test_promote_delayed_once_promotes_when_no_pause_holds() -> None:
    """The other half of the pause assertion: an unpaused job still takes
    the normal path — published once, and NOT re-pushed onto the delayed
    set (which would run it twice)."""
    job_id = uuid.uuid4()
    job = _make_job(id=job_id, tenant_id=uuid.uuid4())

    job_repo = AsyncMock()
    job_repo.get_by_id.return_value = job
    outbox = AsyncMock()
    redis = AsyncMock()
    factory = MagicMock(return_value=_make_session())

    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=outbox),
         ), \
         patch(
             "app.workers.dispatcher.queue.pop_ready_delayed",
             new=AsyncMock(return_value=[str(job_id)]),
         ), \
         patch(
             "app.workers.dispatcher.find_blocking_pause",
             new=AsyncMock(return_value=None),
         ), \
         patch(
             "app.workers.dispatcher.queue.push_delayed", new=AsyncMock()
         ) as mock_push:
        await dispatcher._promote_delayed_once(factory, redis)

    outbox.add.assert_awaited_once()
    assert outbox.add.await_args.kwargs["payload"]["job_id"] == str(job_id)
    mock_push.assert_not_awaited()


async def test_promote_dlq_replay_loop_defers_a_paused_replay() -> None:
    """A scheduled DLQ replay that lands inside a paused DAG must be
    RE-SCHEDULED, not refused.

    This loop's `except` deliberately does not re-enqueue (the operator
    sees the missed replay in the audit trail), so letting `replay_job`'s
    JobError be the mechanism here would silently discard the
    `wait_and_replay` remediation. Probe first, push the replay back onto
    the ZSET, and the deliberate no-re-enqueue policy stays intact for
    real failures.
    """
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    job_id = uuid.uuid4()
    redis = AsyncMock()
    factory = MagicMock(return_value=_make_session())
    replay = AsyncMock()

    with patch(
        "app.workers.dispatcher.dlq_replay_scheduler.claim_ready",
        new=AsyncMock(
            side_effect=[
                [(tenant_id, principal_id, job_id)],
                asyncio.CancelledError(),
            ]
        ),
    ), \
         patch(
             "app.workers.dispatcher.dlq_replay_scheduler.schedule_replay",
             new=AsyncMock(return_value=1_900_000_000.0),
         ) as mock_schedule, \
         patch(
             "app.workers.dispatcher.dlq_replay_scheduler.ack_replay",
             new=AsyncMock(),
         ) as mock_ack, \
         patch(
             "app.workers.dispatcher.find_blocking_pause",
             new=AsyncMock(return_value=uuid.uuid4()),
         ), \
         patch("app.services.job.JobService.replay_job", new=replay), \
         patch("asyncio.sleep", new=AsyncMock()):
        await dispatcher._promote_dlq_replay_loop(factory, redis)

    replay.assert_not_awaited()
    mock_schedule.assert_awaited_once()
    assert mock_schedule.await_args.kwargs["job_id"] == job_id
    assert (
        mock_schedule.await_args.kwargs["delay_seconds"]
        == dispatcher._PAUSED_REPLAY_DEFER_SECONDS
        == 30
    )
    # The deferred entry is re-armed on the scheduled set, so the claim
    # must be released — otherwise it sits in the in-flight set until the
    # TTL lapses and gets replayed a second time.
    mock_ack.assert_awaited_once_with(
        redis, tenant_id=tenant_id, principal_id=principal_id, job_id=job_id
    )


async def test_promote_dlq_replay_once_acks_the_claim_after_a_fired_replay() -> None:
    """R2-21: the happy path releases its claim, so the in-flight set does
    not accumulate entries that a later tick would replay again."""
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    job_id = uuid.uuid4()
    redis = AsyncMock()
    factory = MagicMock(return_value=_make_session())

    with patch(
        "app.workers.dispatcher.dlq_replay_scheduler.claim_ready",
        new=AsyncMock(return_value=[(tenant_id, principal_id, job_id)]),
    ), \
         patch(
             "app.workers.dispatcher.dlq_replay_scheduler.ack_replay",
             new=AsyncMock(),
         ) as mock_ack, \
         patch(
             "app.workers.dispatcher.find_blocking_pause",
             new=AsyncMock(return_value=None),
         ), \
         patch("app.services.job.JobService.replay_job", new=AsyncMock()) as replay:
        await dispatcher._promote_dlq_replay_once(factory, redis)

    replay.assert_awaited_once()
    mock_ack.assert_awaited_once_with(
        redis, tenant_id=tenant_id, principal_id=principal_id, job_id=job_id
    )


async def test_promote_dlq_replay_once_drains_post_commit_hooks() -> None:
    """R2-23 wiring: this loop owns its own `session.begin()`, so it has
    to drain the post-commit queue itself — the `get_db` dependency that
    does it for the API and MCP processes never runs here, and a
    scheduled replay would otherwise leave `GET /jobs/{id}` serving the
    pre-replay status for a full TTL.

    Pinned explicitly because nothing else would notice it going
    missing. Every other test around this function patches `replay_job`
    out, so no hook is ever registered and the drain is a silent no-op —
    which is exactly how the R2-21 refactor that extracted this function
    could have dropped the line without a red test.
    """
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    job_id = uuid.uuid4()
    redis = AsyncMock()
    session = _make_session()
    session.info = {}  # the real `AsyncSession.info`; AsyncMock has none
    factory = MagicMock(return_value=session)
    drained: list[str] = []

    async def _hook() -> None:
        drained.append("invalidated")

    async def _replay_registering_a_hook(*_a: object, **_kw: object) -> None:
        register_post_commit(session, _hook)

    with patch(
        "app.workers.dispatcher.dlq_replay_scheduler.claim_ready",
        new=AsyncMock(return_value=[(tenant_id, principal_id, job_id)]),
    ), \
         patch(
             "app.workers.dispatcher.dlq_replay_scheduler.ack_replay",
             new=AsyncMock(),
         ), \
         patch(
             "app.workers.dispatcher.find_blocking_pause",
             new=AsyncMock(return_value=None),
         ), \
         patch(
             "app.services.job.JobService.replay_job",
             new=_replay_registering_a_hook,
         ):
        await dispatcher._promote_dlq_replay_once(factory, redis)

    assert drained == ["invalidated"]


async def test_promote_dlq_replay_once_acks_a_permanently_failed_replay() -> None:
    """A replay that raises is NOT re-enqueued (unchanged policy — the
    operator sees the `job.replay_scheduled` row with no matching
    `job.replayed` row and re-issues). That policy only holds if the claim
    is released; leaving it in-flight would turn "logged and dropped" into
    "retried forever once the TTL lapses"."""
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    job_id = uuid.uuid4()
    redis = AsyncMock()
    factory = MagicMock(return_value=_make_session())

    with patch(
        "app.workers.dispatcher.dlq_replay_scheduler.claim_ready",
        new=AsyncMock(return_value=[(tenant_id, principal_id, job_id)]),
    ), \
         patch(
             "app.workers.dispatcher.dlq_replay_scheduler.ack_replay",
             new=AsyncMock(),
         ) as mock_ack, \
         patch(
             "app.workers.dispatcher.dlq_replay_scheduler.schedule_replay",
             new=AsyncMock(),
         ) as mock_schedule, \
         patch(
             "app.workers.dispatcher.find_blocking_pause",
             new=AsyncMock(return_value=None),
         ), \
         patch(
             "app.services.job.JobService.replay_job",
             new=AsyncMock(side_effect=RuntimeError("job was deleted")),
         ):
        await dispatcher._promote_dlq_replay_once(factory, redis)

    mock_schedule.assert_not_awaited()
    mock_ack.assert_awaited_once_with(
        redis, tenant_id=tenant_id, principal_id=principal_id, job_id=job_id
    )


async def test_promote_dlq_replay_once_leaves_the_claim_held_when_the_worker_dies() -> None:
    """R2-21 crash window. The old `pop_ready` ZREM'd the whole due batch
    up front, so a worker killed between the pop and the replay discarded
    every remaining entry with no record of the loss.

    A worker death is modelled here as `CancelledError` mid-replay — the
    shutdown signal the dispatcher actually receives. It is a
    BaseException, so it bypasses the per-item `except Exception` and the
    ack that follows it: the entry stays in the in-flight set and a later
    tick reclaims it once the claim TTL lapses. (The reclaim half is
    proven end-to-end against an emulated ZSET in
    `tests/api/test_mcp_dlq_categorization.py`.)"""
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    job_id = uuid.uuid4()
    redis = AsyncMock()
    factory = MagicMock(return_value=_make_session())

    with patch(
        "app.workers.dispatcher.dlq_replay_scheduler.claim_ready",
        new=AsyncMock(return_value=[(tenant_id, principal_id, job_id)]),
    ), \
         patch(
             "app.workers.dispatcher.dlq_replay_scheduler.ack_replay",
             new=AsyncMock(),
         ) as mock_ack, \
         patch(
             "app.workers.dispatcher.find_blocking_pause",
             new=AsyncMock(return_value=None),
         ), \
         patch(
             "app.services.job.JobService.replay_job",
             new=AsyncMock(side_effect=asyncio.CancelledError()),
         ):
        with pytest.raises(asyncio.CancelledError):
            await dispatcher._promote_dlq_replay_once(factory, redis)

    mock_ack.assert_not_awaited()


async def test_run_job_defers_a_paused_job_before_claiming_it() -> None:
    """Pre-claim re-check: a `job.submitted` already in Kafka when the
    pause lands would otherwise claim RUNNING and execute, which is what
    made every promotion-time probe advisory. The job stays PENDING and
    goes back onto `jobs:delayed` for a re-check."""
    job = _make_job()
    factory, job_repo, audit_repo = _make_session_factory(job)
    redis = AsyncMock()
    paused_by = uuid.uuid4()

    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=AsyncMock()),
         ), \
         patch(
             "app.workers.dispatcher.find_blocking_pause",
             new=AsyncMock(return_value=paused_by),
         ), \
         patch(
             "app.workers.dispatcher.kafka_producer.publish_job_progress",
             new=AsyncMock(),
         ) as mock_progress, \
         patch(
             "app.workers.dispatcher.queue.push_delayed", new=AsyncMock()
         ) as mock_push:
        await dispatcher._run_job(str(job.id), factory, redis)

    job_repo.claim_for_running.assert_not_awaited()
    mock_progress.assert_not_awaited()
    mock_push.assert_awaited_once_with(
        redis, str(job.id), dispatcher._PAUSE_RECHECK_SECONDS
    )
