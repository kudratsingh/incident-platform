"""`MAX_JOB_RETRIES` is a real knob, not documentation (WO-R2-76).

`.env.example` has advertised `MAX_JOB_RETRIES=3` as tunable worker
config since the platform shipped, and `Settings.max_job_retries` has
existed to receive it — but nothing ever read the setting. The ceiling
was a literal `3` in three places (the setting's own default, the `jobs`
column default, and `JobService.create_job`'s parameter default), so an
operator who set `MAX_JOB_RETRIES=1` after a bad deploy got three
attempts anyway, and no error to tell them why.

These tests drive the setting through the environment — the way an
operator does — rather than patching the constant, because the bug was
precisely that the environment was disconnected from the behaviour.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import get_settings
from app.models.enums import JobStatus, JobType
from app.models.job import Job
from app.workers import dispatcher


@pytest.fixture
def retries_of(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Set MAX_JOB_RETRIES in the environment and rebuild the settings."""

    def _set(value: int) -> int:
        get_settings.cache_clear()
        monkeypatch.setenv("MAX_JOB_RETRIES", str(value))
        return get_settings().max_job_retries

    yield _set
    get_settings.cache_clear()


def test_setting_is_read_from_the_environment(retries_of) -> None:  # type: ignore[no-untyped-def]
    assert retries_of(1) == 1
    assert retries_of(7) == 7


def test_directly_inserted_job_takes_the_ceiling_from_the_setting(
    retries_of,  # type: ignore[no-untyped-def]
) -> None:
    """The `jobs.max_retries` column default used to be a hardcoded 3.

    This covers every writer that does not go through `JobService` — the
    chaos hooks, the eval seeds, the saga steps — so the knob governs
    them too rather than only the REST creation path.
    """
    retries_of(1)
    job = Job(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        type=JobType.BULK_API_SYNC.value,
        status=JobStatus.PENDING.value,
    )
    # SQLAlchemy resolves a callable column default at flush; the helper
    # the column now points at is what has to read the setting.
    assert Job.__table__.c.max_retries.default.arg(None) == 1

    retries_of(5)
    assert Job.__table__.c.max_retries.default.arg(None) == 5
    assert job is not None  # constructed without an explicit max_retries


async def test_create_job_takes_the_ceiling_from_the_setting(
    retries_of,  # type: ignore[no-untyped-def]
) -> None:
    """`JobService.create_job` had its own `max_retries: int = 3`.

    An explicit argument still wins — the saga coordinator sets a
    per-step ceiling — but the default now comes from the setting."""
    from tests.unit.test_job_service import _make_service

    retries_of(2)
    svc, job_repo, _audit, _outbox = _make_service()
    job_repo.get_by_idempotency_key.return_value = None

    await svc.create_job(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        job_type=JobType.BULK_API_SYNC.value,
    )
    assert job_repo.create.await_args.kwargs["max_retries"] == 2

    # An explicit ceiling still wins over the setting.
    retries_of(2)
    await svc.create_job(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        job_type=JobType.BULK_API_SYNC.value,
        max_retries=9,
    )
    assert job_repo.create.await_args.kwargs["max_retries"] == 9


async def test_ceiling_of_one_dead_letters_on_the_first_failure(
    retries_of,  # type: ignore[no-untyped-def]
) -> None:
    """The operator-visible end of the knob.

    With the ceiling at 1 the dispatcher must dead-letter the first time
    a job fails, instead of scheduling a delayed retry. At HEAD the row
    carried `max_retries=3` no matter what the environment said, so this
    job would have been retried twice more."""
    from app.models.job import _default_max_retries
    from tests.unit.test_dispatcher import _make_job, _make_session_factory

    retries_of(1)
    # Deliberately not the literal 1: the ceiling comes from the same
    # resolver a real INSERT uses, so this closes the chain from the
    # environment variable through to the dispatcher's decision.
    job = _make_job(
        type=JobType.BULK_API_SYNC,
        retry_count=0,
        max_retries=_default_max_retries(),
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


def test_the_ceiling_has_exactly_one_source(retries_of) -> None:  # type: ignore[no-untyped-def]
    """No second literal may drift away from the setting.

    The two former duplicates both resolve through `Settings`, so moving
    the knob moves them together — which is the property the three
    scattered `3`s could not offer."""
    from app.models.job import _default_max_retries
    from app.services import job as job_service

    # The service reads the model's helper — it does not keep a copy.
    assert job_service._default_max_retries is _default_max_retries

    for value in (1, 3, 9):
        retries_of(value)
        assert _default_max_retries() == value
        assert Job.__table__.c.max_retries.default.arg(None) == value
