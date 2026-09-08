"""Tripwire: the docs must describe the attempt budget the code implements.

Three doc lines were wrong at once when WO-R2-172 was filed, and all three
were wrong in the same way — they described a retry budget for a field that
caps runs:

  * `docs/KAFKA.md` called the `job.dlq` field "the job's retry budget".
  * `docs/DATA_MODEL.md` called the column a "per-job cap", which is true of
    either reading and therefore settles nothing.
  * `docs/REDIS.md` said a job waiting on a retry sits in `failed` status —
    stale on a second count, because the retry path writes `pending`.

A rename fixes the code and leaves prose free to drift back, so the rules are
bound to the code here rather than to review: the column name comes from the
model, and the old name is allowed in the docs only on a line that says it is
the old name.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from app.models.job import Job

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

# The docs that describe the budget, plus the runbook that repeated the
# stale REDIS.md sentence verbatim.
_BUDGET_DOCS = ("KAFKA.md", "DATA_MODEL.md", "REDIS.md")

# A line naming the old field is fine when it is explaining that the name
# moved. It is not fine as a plain statement of what the platform has.
_DEPRECATION_MARKERS = ("deprecat", "wo-r2-172", "renamed it", "was called")


def _text(path: str) -> str:
    return (_REPO_ROOT / path).read_text(encoding="utf-8")


def test_the_column_the_docs_must_name_is_the_column_the_model_has() -> None:
    """The anchor for everything below. If the model is renamed again, this
    fails first and says so, instead of the doc assertions failing in a way
    that reads like a docs problem."""
    assert "max_attempts" in Job.__table__.c
    assert "max_retries" not in Job.__table__.c


@pytest.mark.parametrize("doc", _BUDGET_DOCS)
def test_docs_name_the_real_column(doc: str) -> None:
    assert "max_attempts" in _text(f"docs/{doc}"), (
        f"docs/{doc} must name the real column `max_attempts` "
        "(app/models/job.py)"
    )


@pytest.mark.parametrize("doc", (*_BUDGET_DOCS, "ROADMAP.md"))
def test_the_old_name_appears_only_where_it_is_being_retired(doc: str) -> None:
    """`max_retries` is still on the Kafka wire and in the JSON Schema for one
    release, so the docs have to be able to mention it — but only to say that
    it is the deprecated spelling. A bare mention is prose that has drifted
    back to describing a retry count."""
    offenders = [
        line.strip()
        for line in _text(f"docs/{doc}").splitlines()
        if "max_retries" in line
        and not any(m in line.lower() for m in _DEPRECATION_MARKERS)
    ]
    assert not offenders, (
        f"docs/{doc} names `max_retries` without saying it is the deprecated "
        f"name for `max_attempts`: {offenders}"
    )


def test_the_data_model_states_the_arithmetic_in_words() -> None:
    """The finding was that every doc left the off-by-one to be inferred.
    One of them has to spell it out, and DATA_MODEL.md is where the column
    lives."""
    text = _text("docs/DATA_MODEL.md")
    assert "The attempt budget, in words" in text
    assert "the original run plus two retries" in text
    assert "Job exhausted after 3 attempts" in text


@pytest.mark.parametrize(
    "path", ["docs/REDIS.md", "runbooks/rb-redis-memory-low.yaml"]
)
def test_a_job_waiting_on_a_retry_is_not_described_as_failed(path: str) -> None:
    """`_run_job`'s retry branch writes `JobStatus.PENDING`. Both of these
    said `failed`, which sends an operator to a status filter that has never
    held the rows they are looking for."""
    stale = re.compile(r"`failed`[^.\n]{0,40}retry_count\s*<", re.IGNORECASE)
    match = stale.search(_text(path))
    assert match is None, (
        f"{path} still says a job awaiting a retry sits in `failed`; the "
        f"retry path writes `pending` — {match.group(0) if match else ''!r}"
    )
