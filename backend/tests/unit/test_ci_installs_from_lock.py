"""CI installs Python dependencies from the committed lock, never by fresh resolution.

Every Python job used to run `pip install -e ".[dev]"`, which resolves the
newest release of every dependency on every run. On 2026-09-27 SQLAlchemy
2.1.1 removed `sqlalchemy.ext.mypy`, and every open PR went red on mypy
overnight with nothing in the diff (plat #237). `uv.lock` fixes the versions;
`uv sync --locked` installs exactly them and fails if the lock no longer
matches `pyproject.toml`, so a dependency edit cannot land without a re-lock.
Moving a version is now a commit (`uv lock --upgrade-package <name>`), not
something that happens to `master` while nobody is looking (WO-R3-352).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/ci.yml"

# The jobs that need the project's Python environment. A new Python job joins
# this set, and the second test below makes sure it cannot do so silently.
PYTHON_JOBS = {"lint", "test", "integration"}

# Commands that only work after the dependencies are installed.
_NEEDS_ENV = ("pytest", "mypy", "ruff", "lint-imports")


def _jobs() -> dict[str, Any]:
    doc = yaml.safe_load(WORKFLOW.read_text("utf-8"))
    jobs: dict[str, Any] = doc["jobs"]
    return jobs


def _runs(job: dict[str, Any]) -> list[str]:
    return [step["run"] for step in job.get("steps") or [] if isinstance(step.get("run"), str)]


def _words(job: dict[str, Any]) -> set[str]:
    return {word for run in _runs(job) for word in run.split()}


def test_lock_is_committed_at_the_repo_root_and_describes_this_project() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text("utf-8"))
    projects = [p for p in lock["package"] if p.get("source") == {"editable": "."}]
    assert [p["name"] for p in projects] == ["incident-platform"]


def test_no_ci_step_installs_python_packages_by_fresh_resolution() -> None:
    offenders = [
        f"{job_id}: {line.strip()}"
        for job_id, job in _jobs().items()
        for run in _runs(job)
        for line in run.splitlines()
        if "pip install" in line
    ]
    assert not offenders, (
        "a CI step resolves dependencies afresh instead of reading uv.lock — "
        f"use `uv sync --locked --extra dev`: {offenders}"
    )


def test_every_python_job_syncs_from_the_lock() -> None:
    jobs = _jobs()
    uses_env = {job_id for job_id, job in jobs.items() if _words(job) & set(_NEEDS_ENV)}
    assert uses_env == PYTHON_JOBS, (
        "the set of jobs that run Python tooling changed — add the new job to "
        f"PYTHON_JOBS once it installs with `uv sync --locked`: {sorted(uses_env)}"
    )
    for job_id in sorted(PYTHON_JOBS):
        syncs = [run for run in _runs(jobs[job_id]) if "uv sync" in run]
        assert syncs, f"job {job_id!r} never installs from the lock"
        for run in syncs:
            assert "--locked" in run.split(), (
                f"job {job_id!r} runs `uv sync` without --locked, so a stale lock "
                "would pass silently instead of failing the job"
            )
