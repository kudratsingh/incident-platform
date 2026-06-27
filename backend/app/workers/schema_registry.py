"""
JSON Schema registry for Kafka topics.

A file-based registry — schemas are committed alongside the code in
backend/app/schemas/kafka/ and loaded once at import time. Real production
would use Confluent Schema Registry or Apicurio, but the contract here is
the same: every producer validates before publish, every consumer validates
after deserialize, and schema evolution is enforced by versioned $id fields.

Evolution rules (informal for now):
  - Adding optional fields           → backward and forward compatible (allowed).
  - Adding required fields           → breaks consumers; bump $id version.
  - Removing or renaming fields      → breaks consumers; bump $id version.
  - Changing the type of a field     → always breaking; bump $id version.

`additionalProperties: true` in every schema means new fields can appear in
messages without breaking older consumers; the consumers just ignore what
they don't know about.
"""

import json
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.core.logging import get_logger
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

logger = get_logger(__name__)

_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas" / "kafka"

# FormatChecker enables runtime enforcement of "format" keywords like
# "uuid" — by default jsonschema treats them as annotations only.
_FORMAT_CHECKER = FormatChecker()


def _load_all() -> dict[str, Draft202012Validator]:
    """Load every *.schema.json in the schema dir, keyed by topic name.

    Topic <-> filename mapping is taken from the settings module so renaming
    a topic only requires editing config and the file."""
    settings = get_settings()
    mapping = {
        settings.kafka_topic_job_submitted: "job_submitted.schema.json",
        settings.kafka_topic_job_progress: "job_progress.schema.json",
        settings.kafka_topic_job_completed: "job_completed.schema.json",
        settings.kafka_topic_job_failed: "job_failed.schema.json",
        # DLQ uses the same shape as job.failed (with dead_lettered=True).
        settings.kafka_topic_job_dlq: "job_failed.schema.json",
    }
    validators: dict[str, Draft202012Validator] = {}
    for topic, filename in mapping.items():
        path = _SCHEMA_DIR / filename
        with path.open() as f:
            schema = json.load(f)
        Draft202012Validator.check_schema(schema)
        validators[topic] = Draft202012Validator(schema, format_checker=_FORMAT_CHECKER)
    return validators


_VALIDATORS: dict[str, Draft202012Validator] = _load_all()


class SchemaValidationError(ValueError):
    """Raised when a payload fails its topic's schema."""


def validate(topic: str, payload: dict[str, Any]) -> None:
    """Raise SchemaValidationError if `payload` is not valid for `topic`.

    No-op if the topic has no registered schema — keeps callers simple when
    new topics are added before their schemas are written.
    """
    validator = _VALIDATORS.get(topic)
    if validator is None:
        return
    try:
        validator.validate(payload)
    except ValidationError as exc:
        # Re-raise as our own type so callers can catch a stable error class
        # without importing jsonschema directly.
        raise SchemaValidationError(
            f"schema validation failed for {topic}: {exc.message}"
        ) from exc


def reload() -> None:
    """Re-read schemas from disk. Useful for tests that mutate schemas."""
    global _VALIDATORS
    _VALIDATORS = _load_all()
