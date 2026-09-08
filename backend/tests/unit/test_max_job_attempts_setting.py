"""`MAX_JOB_ATTEMPTS` is a real knob, not documentation (WO-R2-76),
and it is named for what it counts (WO-R2-172).

Two findings live in this file because they are the same knob.

WO-R2-76: `.env.example` advertised `MAX_JOB_RETRIES=3` as tunable worker
config since the platform shipped, and a `Settings` field existed to
receive it — but nothing ever read the setting. The ceiling was a literal
`3` in three places (the setting's own default, the `jobs` column
default, and `JobService.create_job`'s parameter default), so an operator
who set it to 1 after a bad deploy got three runs anyway, and no error to
tell them why.

WO-R2-172: the number is a cap on RUNS — the original plus its retries —
because the dispatcher retries while `retry_count < max_attempts`. The
old name promised one run more than the platform has ever given. The
arithmetic is unchanged; `MAX_JOB_RETRIES` is accepted for one release as
a deprecated alias so nobody's tuned ceiling silently reverts to 3.

These tests drive the setting through the environment — the way an
operator does — rather than patching the constant, because the WO-R2-76
bug was precisely that the environment was disconnected from the
behaviour.
"""

from __future__ import annotations

import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import Settings, get_settings
from app.models.enums import JobStatus, JobType
from app.models.job import Job
from app.workers import dispatcher


@pytest.fixture
def attempts_of(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Set MAX_JOB_ATTEMPTS in the environment and rebuild the settings."""

    def _set(value: int) -> int:
        get_settings.cache_clear()
        monkeypatch.setenv("MAX_JOB_ATTEMPTS", str(value))
        return get_settings().max_job_attempts

    yield _set
    get_settings.cache_clear()


def test_setting_is_read_from_the_environment(attempts_of) -> None:  # type: ignore[no-untyped-def]
    assert attempts_of(1) == 1
    assert attempts_of(7) == 7


def test_directly_inserted_job_takes_the_ceiling_from_the_setting(
    attempts_of,  # type: ignore[no-untyped-def]
) -> None:
    """The `jobs.max_attempts` column default used to be a hardcoded 3.

    This covers every writer that does not go through `JobService` — the
    chaos hooks, the eval seeds, the saga steps — so the knob governs
    them too rather than only the REST creation path.
    """
    attempts_of(1)
    job = Job(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        type=JobType.BULK_API_SYNC.value,
        status=JobStatus.PENDING.value,
    )
    # SQLAlchemy resolves a callable column default at flush; the helper
    # the column now points at is what has to read the setting.
    assert Job.__table__.c.max_attempts.default.arg(None) == 1

    attempts_of(5)
    assert Job.__table__.c.max_attempts.default.arg(None) == 5
    assert job is not None  # constructed without an explicit max_attempts


async def test_create_job_takes_the_ceiling_from_the_setting(
    attempts_of,  # type: ignore[no-untyped-def]
) -> None:
    """`JobService.create_job` had its own `max_retries: int = 3`.

    An explicit argument still wins — the saga coordinator sets a
    per-step ceiling — but the default now comes from the setting."""
    from tests.unit.test_job_service import _make_service

    attempts_of(2)
    svc, job_repo, _audit, _outbox = _make_service()
    job_repo.get_by_idempotency_key.return_value = None

    await svc.create_job(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        job_type=JobType.BULK_API_SYNC.value,
    )
    assert job_repo.create.await_args.kwargs["max_attempts"] == 2

    # An explicit ceiling still wins over the setting.
    attempts_of(2)
    await svc.create_job(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        job_type=JobType.BULK_API_SYNC.value,
        max_attempts=9,
    )
    assert job_repo.create.await_args.kwargs["max_attempts"] == 9


async def test_ceiling_of_one_dead_letters_on_the_first_failure(
    attempts_of,  # type: ignore[no-untyped-def]
) -> None:
    """The operator-visible end of the knob.

    With the ceiling at 1 the dispatcher must dead-letter the first time
    a job fails, instead of scheduling a delayed retry — one run, no
    retries. At HEAD before WO-R2-76 the row carried 3 no matter what the
    environment said, so this job would have been retried twice more."""
    from app.models.job import _default_max_attempts
    from tests.unit.test_dispatcher import _make_job, _make_session_factory

    attempts_of(1)
    # Deliberately not the literal 1: the ceiling comes from the same
    # resolver a real INSERT uses, so this closes the chain from the
    # environment variable through to the dispatcher's decision.
    job = _make_job(
        type=JobType.BULK_API_SYNC,
        retry_count=0,
        max_attempts=_default_max_attempts(),
    )
    factory, job_repo, audit_repo = _make_session_factory(job)

    processor = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=AsyncMock()),
         ), \
         patch.dict(dispatcher._PROCESSORS, {JobType.BULK_API_SYNC: processor}), \
         patch(
             "app.workers.dispatcher.queue.push_delayed", new=AsyncMock()
         ) as mock_delay:
        await dispatcher._run_job(str(job.id), factory, AsyncMock())

    mock_delay.assert_not_awaited()
    statuses = [c.args[1] for c in job_repo.update_status.call_args_list]
    assert JobStatus.DEAD_LETTER in statuses
    assert JobStatus.PENDING not in statuses


def test_the_ceiling_has_exactly_one_source(attempts_of) -> None:  # type: ignore[no-untyped-def]
    """No second literal may drift away from the setting.

    The two former duplicates both resolve through `Settings`, so moving
    the knob moves them together — which is the property the three
    scattered `3`s could not offer."""
    from app.models.job import _default_max_attempts
    from app.services import job as job_service

    # The service reads the model's helper — it does not keep a copy.
    assert job_service._default_max_attempts is _default_max_attempts

    for value in (1, 3, 9):
        attempts_of(value)
        assert _default_max_attempts() == value
        assert Job.__table__.c.max_attempts.default.arg(None) == value


# ---------------------------------------------------------------------------
# WO-R2-172: the deprecated `MAX_JOB_RETRIES` alias
# ---------------------------------------------------------------------------
#
# `Settings` is constructed directly here rather than through
# `get_settings()` + monkeypatched env, because what is under test is how
# the model resolves two names — and `Settings(...)` records exactly which
# fields the caller (or the environment) actually set, which is the signal
# the validator reads. Both paths reach the same validator.


def test_new_name_alone_is_the_plain_case() -> None:
    settings = Settings(max_job_attempts=5)
    assert settings.max_job_attempts == 5


def test_old_name_alone_still_sets_the_ceiling() -> None:
    """A deployment that set `MAX_JOB_RETRIES=5` must keep five runs.

    Dropping the alias silently would revert it to the default 3 on the
    release that renamed the knob — the one failure a rename must not
    cause, because it changes behaviour while claiming not to.
    """
    settings = Settings(max_job_retries=5)
    assert settings.max_job_attempts == 5


def test_old_name_alone_logs_one_deprecation_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.config"):
        Settings(max_job_retries=5)
    warnings = [
        r for r in caplog.records if "MAX_JOB_RETRIES is deprecated" in r.getMessage()
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    # The warning has to say what the value means, not just that the name
    # moved — the reader's mental model of "5 retries" is the actual bug.
    assert "MAX_JOB_ATTEMPTS" in message
    assert "5 runs" in message
    assert "4 retries" in message


def test_no_deprecation_warning_when_only_the_new_name_is_set(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.config"):
        Settings(max_job_attempts=5)
    assert not [
        r for r in caplog.records if "MAX_JOB_RETRIES is deprecated" in r.getMessage()
    ]


def test_both_names_set_to_the_same_value_is_allowed() -> None:
    """Mid-migration, both are set and they agree. Unambiguous — take it."""
    settings = Settings(max_job_attempts=5, max_job_retries=5)
    assert settings.max_job_attempts == 5


def test_both_names_set_and_disagreeing_refuses_to_start() -> None:
    """There is no defensible winner, so the process must not pick one.

    Choosing either value runs every job a different number of times than
    half the configuration asked for, and the operator finds out from a
    dead-letter rather than from a message.
    """
    with pytest.raises(ValueError) as exc:
        Settings(max_job_attempts=5, max_job_retries=3)
    message = str(exc.value)
    assert "MAX_JOB_ATTEMPTS" in message
    assert "MAX_JOB_RETRIES" in message
    assert "5" in message and "3" in message


def test_the_alias_reaches_the_column_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through the environment, the way an operator sets it.

    The alias is only worth having if it reaches the place the ceiling is
    actually stamped — otherwise it is a setting nothing reads, which is
    the exact shape of the WO-R2-76 bug this file opened with.
    """
    from app.models.job import _default_max_attempts

    get_settings.cache_clear()
    monkeypatch.delenv("MAX_JOB_ATTEMPTS", raising=False)
    monkeypatch.setenv("MAX_JOB_RETRIES", "4")
    try:
        assert _default_max_attempts() == 4
        assert Job.__table__.c.max_attempts.default.arg(None) == 4
    finally:
        get_settings.cache_clear()
