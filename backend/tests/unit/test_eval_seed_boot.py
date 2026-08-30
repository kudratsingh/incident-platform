"""The SEED_EVAL_FIXTURES boot path and the pin manifest's location.

Live finding (demo stack, 2026-08-16): with SEED_EVAL_FIXTURES=true the
api logged `"eval fixture seed failed", error_type: PermissionError,
[Errno 13] Permission denied: '/app/eval-fixtures-pins.json'` — while
seeding had SUCCEEDED (5 alerts, 9 jobs, 4 dead-lettered, 6 deploy
markers all committed). Two defects hid under that one line:

  * the pins manifest defaulted to `/app/...`, which the shipped image's
    non-root `appuser` cannot write (the Dockerfile COPYs /app as root);
  * the lifespan wrapped `seed()` and `write_pins_json()` in one
    try/except, so a pins-write failure was reported as a seed failure.

These tests pin the fix for both: the default location is runtime-
writable and `EVAL_PINS_PATH`-configurable, and the two failure domains
are unconflatable in the boot log.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from typing import Any

import pytest

# The scripts/ dir isn't a package on disk; make it importable.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_SCRIPTS = os.path.join(_ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


def _seed_module():  # type: ignore[no-untyped-def]
    return importlib.import_module("seed_eval_fixtures")


# ---------------------------------------------------------------------------
# Pin manifest location — writable, configurable
# ---------------------------------------------------------------------------


def test_default_pins_path_is_runtime_writable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default must not live under /app — root-owned in the shipped
    image, so every boot of the pinned image died with EACCES there."""
    seed = _seed_module()
    monkeypatch.delenv("EVAL_PINS_PATH", raising=False)
    path = seed.default_pins_path()
    assert not path.startswith("/app/")
    assert path == os.path.join(tempfile.gettempdir(), "eval-fixtures-pins.json")


def test_eval_pins_path_env_overrides_default(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operators (and the commander's compose) can point the manifest at
    a mount of their choosing; parent dirs are created on the way."""
    seed = _seed_module()
    dest = tmp_path / "mounted" / "pins.json"
    monkeypatch.setenv("EVAL_PINS_PATH", str(dest))

    written = seed.write_pins_json()

    assert written == str(dest)
    manifest = json.loads(dest.read_text())
    # Spot-check the shape scenarios load instead of scraping stdout.
    assert manifest["seed_user_id"] == str(seed.stable("seed-user"))
    assert manifest["dag"]["seed_job_id"] == str(seed.stable("dag-seed-job"))
    assert len(manifest["dlq_jobs"]) == len(seed._dlq_specs())


def test_write_pins_json_raises_oserror_when_unwritable(
    tmp_path: Any,
) -> None:
    """A genuinely unwritable destination surfaces as OSError for the
    caller to log as a *pins* failure — not swallowed, and never able to
    masquerade as a seed failure (seeding is committed before this
    runs)."""
    seed = _seed_module()
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        with pytest.raises(OSError):
            seed.write_pins_json(str(blocked / "sub" / "pins.json"))
    finally:
        blocked.chmod(0o700)


# ---------------------------------------------------------------------------
# Boot logging — the two failure domains stay apart
# ---------------------------------------------------------------------------


class _RecordingLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def info(self, msg: str, **kwargs: Any) -> None:
        self.records.append(("info", msg))

    def error(self, msg: str, **kwargs: Any) -> None:
        self.records.append(("error", msg))

    def messages(self, level: str) -> list[str]:
        return [m for lvl, m in self.records if lvl == level]


async def test_pins_write_failure_is_not_logged_as_seed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact live shape: seed() succeeds, the pins write dies with
    EACCES. The log must say the pins write failed — and must NOT say
    the seed failed."""
    from app import main as app_main

    rec = _RecordingLogger()
    monkeypatch.setattr(app_main, "logger", rec)

    async def _seed_ok(**_kwargs: Any) -> dict[str, int]:
        return {"dlq_reset": 0, "timestamps_rebaselined": 0}

    def _write_pins_denied() -> str:
        raise PermissionError(
            13, "Permission denied", "/app/eval-fixtures-pins.json"
        )

    monkeypatch.setattr(
        app_main, "_import_eval_seeder", lambda: (_seed_ok, _write_pins_denied)
    )

    await app_main._boot_seed_eval_fixtures()

    errors = rec.messages("error")
    assert any("pins write failed" in m for m in errors)
    assert all("seed failed" not in m for m in errors), (
        "a pins-write failure must never be reported as a seed failure — "
        "the fixtures are committed by the time the manifest is written"
    )
    assert "seeded eval fixtures" in rec.messages("info")


async def test_seed_failure_is_still_logged_as_seed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """And the converse: when seeding itself dies, the log says exactly
    that, boot survives, and no pins manifest is written for a world
    that doesn't exist."""
    from app import main as app_main

    rec = _RecordingLogger()
    monkeypatch.setattr(app_main, "logger", rec)

    async def _seed_boom(**_kwargs: Any) -> dict[str, int]:
        raise RuntimeError("db unreachable")

    pins_written: list[bool] = []

    def _write_pins() -> str:
        pins_written.append(True)
        return "/tmp/eval-fixtures-pins.json"

    monkeypatch.setattr(
        app_main, "_import_eval_seeder", lambda: (_seed_boom, _write_pins)
    )

    await app_main._boot_seed_eval_fixtures()

    assert any("seed failed" in m for m in rec.messages("error"))
    assert all("pins" not in m for m in rec.messages("error"))
    assert pins_written == []


async def test_boot_success_logs_seed_and_pins_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as app_main

    rec = _RecordingLogger()
    monkeypatch.setattr(app_main, "logger", rec)

    async def _seed_ok(**_kwargs: Any) -> dict[str, int]:
        return {"dlq_reset": 0, "timestamps_rebaselined": 0}

    monkeypatch.setattr(
        app_main,
        "_import_eval_seeder",
        lambda: (_seed_ok, lambda: "/tmp/eval-fixtures-pins.json"),
    )

    await app_main._boot_seed_eval_fixtures()

    assert rec.messages("error") == []
    infos = rec.messages("info")
    assert "seeded eval fixtures" in infos
    assert any("pins" in m for m in infos)


# ---------------------------------------------------------------------------
# A missing tenant slug degrades; it does not kill the process (WO-R2-69)
# ---------------------------------------------------------------------------


def test_missing_tenant_raises_a_catchable_exception() -> None:
    """`_ensure_tenant` must not raise `SystemExit`.

    `SystemExit` derives from `BaseException`, so the boot guard's
    `except Exception` — whose whole job is to log and let the app start —
    could not catch it. A `SEED_TENANT_SLUG` typo therefore unwound through
    the lifespan and crash-looped the API, for a fixture set the platform is
    designed to run without.
    """
    seed = _seed_module()

    assert issubclass(seed.SeedError, Exception)
    assert not issubclass(seed.SeedError, SystemExit)
    # The guard catches what `_ensure_tenant` raises.
    assert isinstance(seed.SeedError("x"), Exception)


async def test_boot_survives_a_missing_tenant_slug(
    monkeypatch: pytest.MonkeyPatch,
    db_session,  # type: ignore[no-untyped-def]
) -> None:
    """End to end through the real guard, raising from the real function.

    Deliberately calls `_ensure_tenant` rather than raising a stand-in: what
    made this a crash-loop was the exception *class* that one function chose,
    so a test that raises its own exception proves nothing. At HEAD this
    unwinds `SystemExit` straight through `_boot_seed_eval_fixtures` and out
    of the test.
    """
    from app import main as app_main

    seed = _seed_module()
    rec = _RecordingLogger()
    monkeypatch.setattr(app_main, "logger", rec)

    async def _seed_missing_tenant(**_kwargs: Any) -> dict[str, int]:
        await seed._ensure_tenant(db_session, "definitely-not-a-seeded-slug")
        return {}  # pragma: no cover — the line above always raises

    monkeypatch.setattr(
        app_main,
        "_import_eval_seeder",
        lambda: (_seed_missing_tenant, lambda: "/tmp/pins.json"),
    )

    # At HEAD this raised SystemExit straight through the lifespan.
    await app_main._boot_seed_eval_fixtures()

    errors = rec.messages("error")
    assert any("seed failed" in m for m in errors)
