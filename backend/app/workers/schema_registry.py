"""
File-based JSON Schema registry for Kafka topics, loaded once at import from
`backend/app/schemas/kafka/`. Producers validate before publish, consumers after deserialize.

Evolution: adding an optional field is compatible (every schema sets `additionalProperties: true`);
adding a required field, removing or renaming one, or changing a type breaks consumers and needs an
`$id` version bump.
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


#: Prefix identifying a topic field on Settings. Every field with this prefix
#: is a topic that must have a schema; the mapping below is derived from them
#: rather than written out, so a new topic cannot be added without one.
_TOPIC_FIELD_PREFIX = "kafka_topic_"

#: Topics that deliberately reuse another's schema, as {settings field suffix: schema stem}.
#: Anything unlisted derives its filename from its field name: a list of *decisions*.
_SHARED_SCHEMA = {
    # DLQ uses the same shape as job.failed (with dead_lettered=True).
    "job_dlq": "job_failed",
}


class SchemaRegistryError(RuntimeError):
    """Raised at import when a topic in Settings has no schema file."""


def topic_schema_files() -> dict[str, str]:
    """Every `Settings.kafka_topic_*` value mapped to its schema filename, walked off the model so
    "every topic has a schema" is a fact rather than a request."""
    settings = get_settings()
    mapping = {}
    for field in type(settings).model_fields:
        if not field.startswith(_TOPIC_FIELD_PREFIX):
            continue
        suffix = field[len(_TOPIC_FIELD_PREFIX) :]
        stem = _SHARED_SCHEMA.get(suffix, suffix)
        mapping[str(getattr(settings, field))] = f"{stem}.schema.json"
    return mapping


def _load_all() -> dict[str, Draft202012Validator]:
    """Load a validator per configured topic. Raises rather than skipping a missing schema file,
    which would leave that topic silently unvalidated in production."""
    validators: dict[str, Draft202012Validator] = {}
    missing = []
    for topic, filename in topic_schema_files().items():
        path = _SCHEMA_DIR / filename
        if not path.is_file():
            missing.append(f"{topic!r} -> {filename}")
            continue
        with path.open() as f:
            schema = json.load(f)
        Draft202012Validator.check_schema(schema)
        validators[topic] = Draft202012Validator(schema, format_checker=_FORMAT_CHECKER)

    if missing:
        raise SchemaRegistryError(
            "every topic in Settings.kafka_topic_* needs a schema in "
            f"{_SCHEMA_DIR}; missing: {', '.join(sorted(missing))}. Add the "
            "schema file, or map the topic onto an existing one in "
            "_SHARED_SCHEMA if it deliberately reuses that shape."
        )
    return validators


_VALIDATORS: dict[str, Draft202012Validator] = _load_all()


class SchemaValidationError(ValueError):
    """Raised when a payload fails its topic's schema."""


class UnknownTopicError(SchemaValidationError):
    """Raised when `validate` is called for a topic with no registered schema.

    A `SchemaValidationError` subclass on purpose: an unvouchable event takes the existing
    not-publishable / not-consumable path, while staying catchable on its own.
    """


def validate(topic: str, payload: dict[str, Any]) -> None:
    """Raise SchemaValidationError if `payload` is not valid for `topic`, or `UnknownTopicError` if
    the topic has no schema — never success-by-doing-nothing."""
    validator = _VALIDATORS.get(topic)
    if validator is None:
        raise UnknownTopicError(
            f"no schema registered for topic {topic!r}; known topics: "
            f"{sorted(_VALIDATORS)}"
        )
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
