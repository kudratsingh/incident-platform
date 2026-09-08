"""The attempt budget, pinned in words (WO-R2-172).

`jobs.max_attempts` caps how many times a job RUNS — the original run
plus its retries. The dispatcher retries while
`retry_count < max_attempts`, so the default 3 buys three runs and two
retries, and the job dead-letters when the third run fails.

That sentence was true before this work order and is true after it. What
changed is the name: the column was `max_retries`, which told a reader
the ceiling was a retry count, and a reader who believed it budgeted one
run more than the platform has ever given. WO-R2-158's fence columns and
F2-16's badge both had to work around the off-by-one; the retry log line
on the dispatcher's own failure path already printed
`attempt {n}/{max}`, reading the value as attempts.

The other tests in this tree assert the ceiling from one side or the
other — one failure retries, a job at `retry_count = max - 1` dead-letters.
Neither of them counts the runs. This one drives a whole job to its death
and counts, because the count *is* the claim the name now makes, and a
future change to the comparison must fail here loudly rather than quietly
re-baselining an assertion about a single transition.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models.enums import JobStatus, JobType
from app.workers import dispatcher

from .test_dispatcher import _make_job, _make_session_factory


async def _run_to_death(max_attempts: int) -> tuple[AsyncMock, AsyncMock, AsyncMock]:
    """Drive one always-failing job until it dead-letters.

    Returns (processor, push_delayed mock, job_repo). The job mock carries
    its own `retry_count` forward between runs the way the row does: each
    `update_status` writes the count the dispatcher decided on, and the
    next dispatch reads it back. Without that the loop would re-run run 1
    forever and the count would prove nothing.
    """
    job = _make_job(
        type=JobType.BULK_API_SYNC, retry_count=0, max_attempts=max_attempts
    )
    factory, job_repo, audit_repo = _make_session_factory(job)

    def _apply(job_id: object, status: str, **kwargs: object) -> MagicMock:
        extra = kwargs.get("extra") or {}
        if isinstance(extra, dict) and "retry_count" in extra:
            job.retry_count = extra["retry_count"]
        job.status = status
        return job

    job_repo.update_status.side_effect = _apply

    processor = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=AsyncMock()),
         ), \
         patch.dict(dispatcher._PROCESSORS, {JobType.BULK_API_SYNC: processor}), \
         patch("app.workers.dispatcher.queue.push_delayed", new=AsyncMock()) as delay:
        # Bounded well above any sane ceiling so a comparison changed to
        # `<=` overshoots and fails the count rather than hanging here.
        for _ in range(max_attempts + 5):
            await dispatcher._run_job(str(job.id), factory, AsyncMock())
            if job.status == JobStatus.DEAD_LETTER:
                break

    return processor, delay, job_repo


def _statuses(job_repo: AsyncMock) -> list[str]:
    return [c.args[1] for c in job_repo.update_status.call_args_list]


async def test_three_attempts_means_three_runs_and_two_retries() -> None:
    """The default ceiling, counted. Three runs, two retries, one death."""
    processor, delay, job_repo = await _run_to_death(3)

    assert processor.await_count == 3, (
        "max_attempts=3 must buy exactly three RUNS of the processor — the "
        "original plus two retries"
    )
    assert delay.await_count == 2, (
        "three runs are separated by exactly two scheduled retries"
    )
    statuses = _statuses(job_repo)
    assert statuses.count(JobStatus.DEAD_LETTER) == 1
    assert statuses.count(JobStatus.PENDING) == 2


async def test_a_ceiling_of_one_means_one_run_and_no_retries() -> None:
    """The degenerate end of the same sentence, which is where the old name
    was most misleading: `max_retries=1` reads as "retry it once"."""
    processor, delay, job_repo = await _run_to_death(1)

    assert processor.await_count == 1
    assert delay.await_count == 0
    assert JobStatus.PENDING not in _statuses(job_repo)
    assert JobStatus.DEAD_LETTER in _statuses(job_repo)


@pytest.mark.parametrize("ceiling", [1, 2, 3, 5])
async def test_runs_always_equal_the_ceiling_and_retries_are_one_fewer(
    ceiling: int,
) -> None:
    """Stated as the general rule so a future edit to the comparison shows
    up as arithmetic, not as one broken example."""
    processor, delay, _job_repo = await _run_to_death(ceiling)

    assert processor.await_count == ceiling
    assert delay.await_count == ceiling - 1


async def test_the_retry_message_counts_runs_in_words() -> None:
    """`attempt {n} of {max}` — one word, matching the dead-letter line's
    "exhausted after N attempts" and the lab's seeded `attempt 3/3` texts.
    The slash form was the shape a reader most often mistook for a retry
    count, so the wording spells the relationship out.
    """
    job = _make_job(type=JobType.BULK_API_SYNC, retry_count=0, max_attempts=3)
    factory, job_repo, audit_repo = _make_session_factory(job)
    outbox = AsyncMock()

    processor = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("app.workers.dispatcher.JobRepository", return_value=job_repo), \
         patch("app.workers.dispatcher.AuditRepository", return_value=audit_repo), \
         patch(
             "app.workers.dispatcher.OutboxRepository",
             new=MagicMock(return_value=outbox),
         ), \
         patch.dict(dispatcher._PROCESSORS, {JobType.BULK_API_SYNC: processor}), \
         patch("app.workers.dispatcher.queue.push_delayed", new=AsyncMock()):
        await dispatcher._run_job(str(job.id), factory, AsyncMock())

    message = outbox.add.await_args.kwargs["payload"]["message"]
    assert "attempt 1 of 3" in message
    assert "1/3" not in message
