"""Unit tests for the worker dispatcher — DB and Redis fully mocked."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.enums import JobStatus, JobType
from app.models.job import Job
from app.workers import dispatcher


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


def _make_session_factory(job: MagicMock) -> MagicMock:
    """Returns a session factory whose sessions yield a job_repo that returns `job`."""
    job_repo = AsyncMock()
    job_repo.get_by_id.return_value = job
    job_repo.update_status.return_value = job

    audit_repo = AsyncMock()
    audit_repo.log = AsyncMock()

    # session.begin() must be a regular (sync) call returning an async context manager
    begin_ctx = MagicMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_ctx)

    factory = MagicMock()
    factory.return_value = session

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


async def test_run_job_dead_letters_after_exhaustion() -> None:
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
         patch("app.workers.dispatcher.queue.push_delayed", new=AsyncMock()) as mock_delay:
        await dispatcher._run_job(str(job.id), factory, redis)

    mock_delay.assert_not_awaited()
    calls = [c.args[1] for c in job_repo.update_status.call_args_list]
    assert JobStatus.DEAD_LETTER in calls


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
    """
    outbox_mock = AsyncMock()
    outbox_ctor = MagicMock(return_value=outbox_mock)

    # `type` is the compensate string, not a JobType member.
    job = _make_job(type="csv_upload.compensate", retry_count=0, max_retries=3)
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

    # An outbox row was enqueued on the DLQ topic with dead_lettered=True
    # so the saga coordinator can settle the saga.
    outbox_mock.add.assert_awaited()
    outbox_payload = outbox_mock.add.await_args.kwargs["payload"]
    assert outbox_payload["dead_lettered"] is True
    assert outbox_payload["job_type"] == "csv_upload.compensate"

    # And an audit row was written so the incident is visible in /audit/logs.
    audit_repo.log.assert_awaited()
    action = audit_repo.log.await_args.args[0]
    assert action == "job.dead_letter"


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
    """
    factory, job_repo, audit_repo = _make_session_factory(_make_job())
    redis = AsyncMock()
    consumer = dispatcher.JobDispatcherConsumer(factory, redis)
    consumer.session_factory = factory
    consumer.redis = redis

    job_id = str(uuid.uuid4())

    with patch(
        "app.workers.dispatcher._run_job",
        new=AsyncMock(side_effect=RuntimeError("boom past guards")),
    ), patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
       patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo):
        # Must NOT re-raise.
        await consumer._run_and_release(job_id)

    # The safety net force-dead-lettered the job.
    calls = [c.args[1] for c in job_repo.update_status.call_args_list]
    assert JobStatus.DEAD_LETTER in calls


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
