"""Keep the public environment template complete without reading real secrets."""

import ast
import re
from pathlib import Path

import pytest
from app.config import Settings

ROOT = Path(__file__).resolve().parents[3]


def test_every_settings_field_is_documented() -> None:
    text = (ROOT / ".env.example").read_text()
    # A commented assignment documents optional settings without enabling them.
    names = set(re.findall(r"^\s*(?:#\s*)?([A-Z][A-Z0-9_]*)=", text, re.M))
    missing = {name.upper() for name in Settings.model_fields} - names
    assert not missing, f"Settings missing from .env.example: {sorted(missing)}"
    assert {"ANTHROPIC_API_KEY", "ALEMBIC_DATABASE_URL"} <= names


def test_ci_census_covers_every_integration_module() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    match = re.search(r"EXPECTED = (\{[^}]+\})", workflow)
    assert match is not None, "integration census EXPECTED set missing"
    expected = ast.literal_eval(match.group(1))
    modules = {p.stem for p in (ROOT / "backend/tests/integration").glob("test_*.py")}
    assert expected == modules, "CI census must name every integration file"


def test_template_parses_with_paid_and_lab_features_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Use only this public template: never consult a developer's environment
    # or .env file, and never enable a feature that could spend or inject faults.
    monkeypatch.setattr(
        Settings,
        "settings_customise_sources",
        classmethod(lambda cls, settings_cls, **sources: (sources["dotenv_settings"],)),
    )
    settings = Settings(_env_file=ROOT / ".env.example")
    assert settings.cors_origins == ["http://localhost:3000", "http://127.0.0.1:3000"]
    assert not settings.chaos_enabled
    assert not settings.seed_eval_fixtures
    assert not any(
        getattr(settings, name)
        for name in Settings.model_fields
        if name.startswith("llm_") and name.endswith("_enabled")
    )
    assert settings.alert_webhook_url is None
    assert settings.alert_webhook_secret is None
