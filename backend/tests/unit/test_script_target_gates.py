"""The scripts/ safety gate: WO-R2-18 (gate the reset on its target)
and WO-R2-19 (gate the seeders, floor the credential lifetime).

The property under test throughout is that a refusal happens *before
anything is connected*. Every one of these scripts takes its target as
an argument or an envvar and then destroys or overwrites data there, so
"it raised eventually" is not the guarantee anyone needs — "it never
opened a connection" is. Each test therefore replaces
`create_async_engine` with a callable that fails the test if it is
reached, which is a strictly stronger assertion than counting rows
afterwards: no engine means no statement means no rows touched.

The old gate read `settings.environment` and nothing else. An operator
in a `development` shell — the normal way these scripts are run —
passed it unconditionally, and then the `DELETE FROM jobs` landed on
whatever `DATABASE_URL` was set. The label described the shell; the DSN
chose the victim."""

from __future__ import annotations

import importlib
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_SCRIPTS = os.path.join(_ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

# The DSN both `Settings` and every script default to. Tests that need a
# "configured" target use this; tests that need a mismatch vary the host.
_CONFIGURED_DB = "postgresql+asyncpg://postgres:postgres@localhost:5432/incident_platform"
_CONFIGURED_REDIS = "redis://localhost:6379/0"
_OTHER_DB = "postgresql+asyncpg://postgres:postgres@prod-db.internal:5432/incident_platform"


def _safety():  # type: ignore[no-untyped-def]
    return importlib.import_module("eval_safety")


def _reset_module():  # type: ignore[no-untyped-def]
    return importlib.import_module("reset_eval_state")


def _seed_module():  # type: ignore[no-untyped-def]
    return importlib.import_module("seed_eval_fixtures")


def _load_test_users_module():  # type: ignore[no-untyped-def]
    return importlib.import_module("seed_load_test_users")


def _commander_module():  # type: ignore[no-untyped-def]
    return importlib.import_module("seed_incident_commander")


@pytest.fixture
def clean_settings(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """`get_settings` is LRU-cached; every test here overrides env vars
    that feed it, so the cache is cleared on the way in and out."""
    from app.config import get_settings

    monkeypatch.setenv("ENVIRONMENT", "development")
    get_settings.cache_clear()
    yield get_settings
    get_settings.cache_clear()


def _no_engine(*_a, **_k):  # type: ignore[no-untyped-def]
    raise AssertionError(
        "the gate must refuse before create_async_engine — a script that "
        "connects first has already chosen its target"
    )


# ---------------------------------------------------------------------------
# eval_safety._identity — what counts as "the same target"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # Driver suffix is not part of the target's identity.
        (
            "postgresql://postgres:postgres@localhost:5432/incident_platform",
            "postgresql+asyncpg://postgres:postgres@localhost:5432/incident_platform",
        ),
        # Rotated password, same rows.
        (
            "postgresql+asyncpg://postgres:hunter2@localhost:5432/incident_platform",
            "postgresql+asyncpg://postgres:postgres@localhost:5432/incident_platform",
        ),
        # Different user, same database.
        (
            "postgresql+asyncpg://app_rw:pw@localhost:5432/incident_platform",
            "postgresql+asyncpg://postgres:postgres@localhost:5432/incident_platform",
        ),
        # Default port omitted.
        (
            "postgresql+asyncpg://postgres:postgres@localhost/incident_platform",
            "postgresql+asyncpg://postgres:postgres@localhost:5432/incident_platform",
        ),
        # Connection options don't change which database it is.
        (
            "postgresql+asyncpg://postgres:postgres@localhost:5432/incident_platform?sslmode=require",
            "postgresql+asyncpg://postgres:postgres@localhost:5432/incident_platform",
        ),
        # `postgres://` is an alias of `postgresql://`.
        (
            "postgres://postgres:postgres@localhost:5432/incident_platform",
            "postgresql://postgres:postgres@localhost:5432/incident_platform",
        ),
        (
            "redis://localhost/0",
            "redis://localhost:6379/0",
        ),
    ],
)
def test_cosmetic_dsn_differences_are_the_same_target(a: str, b: str) -> None:
    """False refusals cost as much as false accepts here: a gate that
    fires on a driver swap gets an `--i-know-what-im-doing` baked into
    the Makefile, and then it is not a gate any more."""
    assert _safety()._identity(a) == _safety()._identity(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # Different host — the whole point.
        (
            "postgresql+asyncpg://postgres:postgres@localhost:5432/incident_platform",
            "postgresql+asyncpg://postgres:postgres@prod-db.internal:5432/incident_platform",
        ),
        # Different database on the same server.
        (
            "postgresql+asyncpg://postgres:postgres@localhost:5432/incident_platform",
            "postgresql+asyncpg://postgres:postgres@localhost:5432/incident_platform_prod",
        ),
        # Different port — a tunnel to somewhere else.
        (
            "postgresql+asyncpg://postgres:postgres@localhost:5432/incident_platform",
            "postgresql+asyncpg://postgres:postgres@localhost:5433/incident_platform",
        ),
        # Different Redis logical db.
        ("redis://localhost:6379/0", "redis://localhost:6379/1"),
    ],
)
def test_material_dsn_differences_are_a_different_target(a: str, b: str) -> None:
    assert _safety()._identity(a) != _safety()._identity(b)


def test_redact_hides_the_password_and_keeps_the_host() -> None:
    """These lines land in CI logs and eval reports."""
    redacted = _safety().redact(
        "postgresql+asyncpg://postgres:hunter2@db.internal:5432/incident_platform"
    )
    assert "hunter2" not in redacted
    assert "db.internal:5432" in redacted
    assert "incident_platform" in redacted


def test_describe_target_names_both_backends() -> None:
    described = _safety().describe_target(_CONFIGURED_DB, _CONFIGURED_REDIS)
    assert "localhost:5432/incident_platform" in described
    assert "localhost:6379/0" in described


# ---------------------------------------------------------------------------
# WO-R2-18 — reset() is gated on the database it is about to destroy
# ---------------------------------------------------------------------------


async def test_reset_refuses_a_database_url_that_is_not_the_configured_one(
    monkeypatch: pytest.MonkeyPatch, clean_settings
) -> None:
    """The headline of WO-R2-18.

    `ENVIRONMENT=development` here — the gate that existed before this
    change passes cleanly, and every destructive statement then runs
    against `prod-db.internal`. Nothing about the old check could tell
    the difference, because it never looked at the argument."""
    reset = _reset_module()
    monkeypatch.setattr(reset, "create_async_engine", _no_engine)

    with pytest.raises(RuntimeError) as exc_info:
        await reset.reset(database_url=_OTHER_DB, redis_url=_CONFIGURED_REDIS)

    message = str(exc_info.value)
    assert "refuses to run against a database_url" in message
    # The message has to name both DSNs or the operator can't tell which
    # of the two is the one they got wrong.
    assert "prod-db.internal" in message
    assert "--i-know-what-im-doing" in message


async def test_reset_refuses_a_redis_url_that_is_not_the_configured_one(
    monkeypatch: pytest.MonkeyPatch, clean_settings
) -> None:
    """Redis is gated too: `_clear_chaos_keys` scan-and-deletes a whole
    namespace, and it runs before any of the SQL."""
    reset = _reset_module()
    monkeypatch.setattr(reset, "create_async_engine", _no_engine)

    with pytest.raises(RuntimeError, match="refuses to run against a redis_url"):
        await reset.reset(
            database_url=_CONFIGURED_DB, redis_url="redis://cache.internal:6379/0"
        )


async def test_reset_production_check_still_precedes_the_target_check(
    monkeypatch: pytest.MonkeyPatch, clean_settings
) -> None:
    """Order matters for the operator, not for safety: both refuse. If
    the target check ran first, someone pointed at production with a
    matching DSN would be told about DSNs rather than about
    production."""
    reset = _reset_module()
    from app.config import get_settings

    monkeypatch.setattr(reset, "create_async_engine", _no_engine)
    monkeypatch.setenv("ENVIRONMENT", "production")
    # The Settings validator rejects the default SECRET_KEY under
    # production; feed it a long enough one so we reach the guardrail.
    monkeypatch.setenv("SECRET_KEY", "a" * 48)
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="refuses to run in production"):
        await reset.reset(database_url=_OTHER_DB, redis_url=_CONFIGURED_REDIS)


async def test_reset_allows_a_deliberate_mismatch_behind_the_flag(
    monkeypatch: pytest.MonkeyPatch, clean_settings
) -> None:
    """`allow_target_mismatch=True` is the documented escape hatch, so
    it must actually get past the gate — asserted by letting the engine
    constructor be reached and failing there instead."""
    reset = _reset_module()

    class _Reached(Exception):
        pass

    def _engine(*_a, **_k):  # type: ignore[no-untyped-def]
        raise _Reached()

    monkeypatch.setattr(reset, "create_async_engine", _engine)

    with pytest.raises(_Reached):
        await reset.reset(
            database_url=_OTHER_DB,
            redis_url=_CONFIGURED_REDIS,
            allow_target_mismatch=True,
        )


async def test_reset_override_does_not_unlock_production(
    monkeypatch: pytest.MonkeyPatch, clean_settings
) -> None:
    """One lever per invariant. `--i-know-what-im-doing` is about the
    target; overriding `ENVIRONMENT` remains the only way past the
    production check, as it was before."""
    reset = _reset_module()
    from app.config import get_settings

    monkeypatch.setattr(reset, "create_async_engine", _no_engine)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "a" * 48)
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="refuses to run in production"):
        await reset.reset(
            database_url=_CONFIGURED_DB,
            redis_url=_CONFIGURED_REDIS,
            allow_target_mismatch=True,
        )


def test_reset_cli_exits_1_on_a_target_mismatch(
    monkeypatch: pytest.MonkeyPatch, clean_settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI contract `make eval-reset` reads: stderr + exit 1."""
    reset = _reset_module()

    with pytest.raises(SystemExit) as exc_info:
        reset._refuse_in_production(_OTHER_DB, _CONFIGURED_REDIS)

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "refuses to run against a database_url" in err
    assert "prod-db.internal" in err


# ---------------------------------------------------------------------------
# WO-R2-19 — the seeder family gets the same gate
# ---------------------------------------------------------------------------


async def test_seed_eval_fixtures_refuses_an_unconfigured_target(
    monkeypatch: pytest.MonkeyPatch, clean_settings
) -> None:
    seed = _seed_module()
    monkeypatch.setattr(seed, "create_async_engine", _no_engine)

    with pytest.raises(RuntimeError, match="refuses to run against a database_url"):
        await seed.seed(database_url=_OTHER_DB, redis_url=_CONFIGURED_REDIS)


async def test_seed_load_test_users_refuses_an_unconfigured_target(
    monkeypatch: pytest.MonkeyPatch, clean_settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """The finding as filed: this script creates a `role=admin` account
    with a password published in the repo, and before WO-R2-19 it wrote
    it to whatever `DATABASE_URL` was set with no gate at all."""
    module = _load_test_users_module()
    monkeypatch.setattr(module, "_DB_URL", _OTHER_DB)
    monkeypatch.setattr(module, "create_async_engine", _no_engine)
    monkeypatch.setattr(sys, "argv", ["seed_load_test_users.py"])

    with pytest.raises(SystemExit) as exc_info:
        await module.main()

    assert exc_info.value.code == 1
    assert "refuses to run against a database_url" in capsys.readouterr().err


async def test_seed_load_test_users_refuses_in_production(
    monkeypatch: pytest.MonkeyPatch, clean_settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """Even pointed at the configured database: an admin account with a
    repo-published password does not belong in a production row."""
    module = _load_test_users_module()
    from app.config import get_settings

    monkeypatch.setattr(module, "_DB_URL", _CONFIGURED_DB)
    monkeypatch.setattr(module, "create_async_engine", _no_engine)
    monkeypatch.setattr(sys, "argv", ["seed_load_test_users.py"])
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "a" * 48)
    get_settings.cache_clear()

    with pytest.raises(SystemExit) as exc_info:
        await module.main()

    assert exc_info.value.code == 1
    assert "refuses to run in production" in capsys.readouterr().err


async def test_seed_load_test_users_prints_the_target_before_writing(
    monkeypatch: pytest.MonkeyPatch, clean_settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """An operator minting an admin account should not have to read
    their own shell history to find out where it went."""
    module = _load_test_users_module()

    class _Reached(Exception):
        pass

    def _engine(*_a, **_k):  # type: ignore[no-untyped-def]
        raise _Reached()

    monkeypatch.setattr(module, "_DB_URL", _CONFIGURED_DB)
    monkeypatch.setattr(module, "create_async_engine", _engine)
    monkeypatch.setattr(sys, "argv", ["seed_load_test_users.py"])

    with pytest.raises(_Reached):
        await module.main()

    assert "localhost:5432/incident_platform" in capsys.readouterr().out


async def test_seed_incident_commander_refuses_an_unconfigured_target(
    monkeypatch: pytest.MonkeyPatch, clean_settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """This one mints a live bearer token, so "which database" decides
    which tenant's data the resulting credential can read."""
    module = _commander_module()
    monkeypatch.setattr(module, "_DB_URL", _OTHER_DB)
    monkeypatch.setattr(module, "_TTL_DAYS_ENV", None)
    monkeypatch.setattr(module, "create_async_engine", _no_engine)
    monkeypatch.setattr(sys, "argv", ["seed_incident_commander.py"])

    with pytest.raises(SystemExit) as exc_info:
        await module.main()

    assert exc_info.value.code == 1
    assert "refuses to run against a database_url" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# WO-R2-19 — SA_TTL_DAYS credential-lifetime floor
# ---------------------------------------------------------------------------


def test_ttl_days_unset_means_platform_default() -> None:
    parse = _commander_module()._parse_ttl_days
    assert parse(None) is None
    assert parse("") is None
    assert parse("   ") is None


@pytest.mark.parametrize("raw", ["1", "30", "90", "365"])
def test_ttl_days_accepts_the_api_range(raw: str) -> None:
    """Same bound as `MintTokenRequest.ttl_days` (ge=1, le=365) — a
    script that mints the identical credential should not accept values
    the endpoint rejects."""
    assert _commander_module()._parse_ttl_days(raw) == int(raw)


def test_ttl_days_zero_is_rejected_rather_than_silently_widened() -> None:
    """The finding: `SA_TTL_DAYS=0` was parsed to `0`, then read as
    falsy by `timedelta(days=ttl) if ttl else None`, and minted the
    **90-day default** — the operator asked for the shortest possible
    lifetime and got the longest one, with the plaintext token printed
    as if nothing had happened."""
    with pytest.raises(ValueError) as exc_info:
        _commander_module()._parse_ttl_days("0")

    message = str(exc_info.value)
    assert "SA_TTL_DAYS=0" in message
    assert "No token was minted." in message


@pytest.mark.parametrize("raw", ["-5", "366", "1000"])
def test_ttl_days_rejects_out_of_range(raw: str) -> None:
    """`-5` was the quieter half of the same bug: negative is truthy, so
    it minted a token that had already expired."""
    with pytest.raises(ValueError, match="Valid range is 1-365"):
        _commander_module()._parse_ttl_days(raw)


@pytest.mark.parametrize("raw", ["ninety", "30d", "1.5", ""])
def test_ttl_days_rejects_non_integers(raw: str) -> None:
    if raw == "":
        assert _commander_module()._parse_ttl_days(raw) is None
        return
    with pytest.raises(ValueError, match="whole number of days"):
        _commander_module()._parse_ttl_days(raw)


async def test_ttl_days_zero_exits_non_zero_without_minting(
    monkeypatch: pytest.MonkeyPatch, clean_settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end shape of the fix: non-zero exit, one clear line, and
    — the part that matters — no engine, so no token row and no
    plaintext on stdout to be pasted into a `.env`."""
    module = _commander_module()
    monkeypatch.setattr(module, "_DB_URL", _CONFIGURED_DB)
    monkeypatch.setattr(module, "_TTL_DAYS_ENV", "0")
    monkeypatch.setattr(module, "create_async_engine", _no_engine)
    monkeypatch.setattr(sys, "argv", ["seed_incident_commander.py"])

    with pytest.raises(SystemExit) as exc_info:
        await module.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "SA_TTL_DAYS=0 is invalid" in captured.err
    assert "PLATFORM_TOKEN" not in captured.out


def test_owner_and_app_role_dsns_are_the_same_target() -> None:
    """The two-URL scheme (WO-P2-03 / ADR 0015) in one assertion.

    The runtime `DATABASE_URL` is the non-owner `incident_app` role; the
    owner URL is `postgres:postgres`. They differ only in credentials and
    name the same database, and ad-hoc ops work runs through the owner
    URL. A gate that compared usernames would refuse that every time,
    and the fix for *that* would be an `--i-know-what-im-doing` baked
    into the Makefile — at which point there is no gate left."""
    safety = _safety()
    assert safety._identity(
        "postgresql+asyncpg://incident_app:localdev@localhost:5432/incident_platform"
    ) == safety._identity(
        "postgresql+asyncpg://postgres:postgres@localhost:5432/incident_platform"
    )


async def test_reset_cli_keeps_stdout_parseable_as_json(
    monkeypatch: pytest.MonkeyPatch, clean_settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """`make eval-reset` parses this script's stdout into the eval
    report, so the new "here is your target" line goes to stderr. A
    banner on stdout would break the caller silently — it would still
    exit 0."""
    import json

    reset = _reset_module()
    summary = {"dlq_swept": 0, "chaos_keys_cleared": 0}

    async def _fake_reset(**_kwargs):  # type: ignore[no-untyped-def]
        return summary

    monkeypatch.setattr(reset, "reset", _fake_reset)
    monkeypatch.setattr(reset, "_DB_URL", _CONFIGURED_DB)
    monkeypatch.setattr(reset, "_REDIS_URL", _CONFIGURED_REDIS)
    monkeypatch.setattr(sys, "argv", ["reset_eval_state.py"])

    await reset.main()

    captured = capsys.readouterr()
    assert json.loads(captured.out) == summary
    assert "localhost:5432/incident_platform" in captured.err
