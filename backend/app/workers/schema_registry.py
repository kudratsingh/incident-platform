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


#: Prefix identifying a topic field on Settings. Every field with this prefix
#: is a topic that must have a schema; the mapping below is derived from them
#: rather than written out, so a new topic cannot be added without one.
_TOPIC_FIELD_PREFIX = "kafka_topic_"

#: Topics that deliberately reuse another topic's schema, as
#: {settings field suffix: schema stem}. Everything not listed here derives
#: its own filename from its field name, so this stays a list of *decisions*
#: rather than a copy of the topic list — the shape the old hand-written dict
#: had, where an omission was indistinguishable from a topic with no schema.
_SHARED_SCHEMA = {
    # DLQ uses the same shape as job.failed (with dead_lettered=True).
    "job_dlq": "job_failed",
}


class SchemaRegistryError(RuntimeError):
    """Raised at import when a topic in Settings has no schema file."""


def topic_schema_files() -> dict[str, str]:
    """Every `Settings.kafka_topic_*` value mapped to its schema filename.

    Walks the Settings model rather than repeating its fields. CLAUDE.md
    states that every topic in `Settings.kafka_topic_*` must have a matching
    `.schema.json`; deriving the mapping is what makes that a fact about the
    code instead of a request to whoever adds the next topic.
    """
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
    """Load a validator per configured topic, keyed by topic name.

    Raises rather than skipping a topic whose schema file is missing. The
    alternative — registering what exists and leaving the rest unvalidated —
    is the failure this guard exists to prevent, and it fails at the worst
    possible moment: silently, in production, one topic at a time. Failing at
    import turns it into a boot error on the deploy that introduced it.
    """
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

    A subclass of SchemaValidationError on purpose: both callers already treat
    that as "this message is not publishable / not consumable" and handle it
    (the producer logs and drops, the consumer commits past the poison pill).
    An unmapped topic is the same situation — an event nobody can vouch for —
    so it takes the same path rather than needing new handling at every call
    site, while still being catchable on its own where the distinction matters.
    """


def validate(topic: str, payload: dict[str, Any]) -> None:
    """Raise SchemaValidationError if `payload` is not valid for `topic`.

    Raises UnknownTopicError for a topic with no schema. This used to return
    silently, which meant an unregistered topic got *no* validation at all
    while every call site believed it had been validated — the check reported
    success by doing nothing. `_load_all` makes that state unreachable for
    topics declared in Settings; this covers a topic name passed as a bare
    string from somewhere else.
    """
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
