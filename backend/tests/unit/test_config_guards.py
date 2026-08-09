"""Guard-matrix tests for the fail-closed production secret checks on Settings.

Findings covered (WO-P6-01):

- E2-08 — the old ``secret_key`` guard read ``os.getenv("ENVIRONMENT")``
  instead of the parsed setting, so ``ENVIRONMENT=production`` supplied only
  via the ``.env`` file (a supported config source) bypassed it and the
  insecure default JWT key was silently accepted.
- E2-07 — ``storage_access_key`` / ``storage_secret_key`` shipped the usable
  default ``minioadmin`` with no production guard at all.

Every construction passes ``_env_file=None`` (or an explicit tmp file) so a
developer's local ``.env`` cannot make the suite non-hermetic.
"""

from pathlib import Path

import pytest
from app.config import _INSECURE_DEFAULT_KEY, Settings
from pydantic import ValidationError

_STRONG_KEY = "x" * 48


@pytest.fixture(autouse=True)
def _hermetic_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip config-relevant process env vars so each case exercises exactly
    the config source it claims to (init kwargs or an explicit .env file)."""
    for var in (
        "ENVIRONMENT",
        "SECRET_KEY",
        "STORAGE_ACCESS_KEY",
        "STORAGE_SECRET_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# E2-08 — the guard must validate the *parsed* environment, whatever its
# config source, not os.getenv.
# ---------------------------------------------------------------------------


def test_production_kwarg_with_default_secret_key_is_refused() -> None:
    """The exact case the os.getenv guard let through: no ENVIRONMENT process
    var (the old code saw None and defaulted to 'development') while the
    parsed setting says production."""
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(_env_file=None, environment="production")


def test_production_via_env_file_only_is_refused(tmp_path: Path) -> None:
    """The literal reported bypass: ENVIRONMENT=production supplied ONLY via
    the .env file, per SettingsConfigDict(env_file='.env')."""
    env_file = tmp_path / ".env"
    env_file.write_text("ENVIRONMENT=production\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(_env_file=str(env_file))


# ---------------------------------------------------------------------------
# E2-07 — the weak default MinIO credential must never reach production.
# ---------------------------------------------------------------------------


def test_production_refuses_minioadmin_storage_secret_key() -> None:
    with pytest.raises(ValidationError, match="STORAGE_SECRET_KEY"):
        Settings(
            _env_file=None,
            environment="production",
            secret_key=_STRONG_KEY,
            storage_secret_key="minioadmin",
        )


def test_production_refuses_minioadmin_storage_access_key() -> None:
    with pytest.raises(ValidationError, match="STORAGE_ACCESS_KEY"):
        Settings(
            _env_file=None,
            environment="production",
            secret_key=_STRONG_KEY,
            storage_access_key="minioadmin",
        )


# ---------------------------------------------------------------------------
# Negative space — the shapes that must keep booting.
# ---------------------------------------------------------------------------


def test_production_with_strong_secret_and_iam_storage_boots() -> None:
    """The real ECS shape: strong SECRET_KEY, no storage credentials — the
    task IAM role provides S3 access (infra injects only STORAGE_BUCKET)."""
    settings = Settings(
        _env_file=None, environment="production", secret_key=_STRONG_KEY
    )
    assert settings.storage_access_key is None
    assert settings.storage_secret_key is None


def test_development_with_all_defaults_boots() -> None:
    settings = Settings(_env_file=None)
    assert settings.environment == "development"
    assert settings.secret_key == _INSECURE_DEFAULT_KEY


def test_no_usable_storage_credential_default_ships() -> None:
    """E2-07's latent half: even outside production, no usable credential
    literal may ship as a default."""
    settings = Settings(_env_file=None)
    assert settings.storage_access_key is None
    assert settings.storage_secret_key is None
