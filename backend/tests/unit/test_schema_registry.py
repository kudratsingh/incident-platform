"""Unit tests for the JSON Schema registry."""

import uuid
from pathlib import Path

import pytest
from app.config import get_settings
from app.workers import schema_registry
from app.workers.schema_registry import (
    SchemaValidationError,
    UnknownTopicError,
    topic_schema_files,
)
from app.workers.schema_registry import validate as validate_schema


def test_valid_job_submitted_passes() -> None:
    validate_schema(
        "job.submitted",
        {
            "event": "job.submitted",
            "tenant_id": str(uuid.uuid4()),
            "job_id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
            "job_type": "csv_upload",
            "payload": {"file": "x.csv"},
            "priority": 0,
            "trace_id": None,
        },
    )


def test_missing_required_field_raises() -> None:
    with pytest.raises(SchemaValidationError):
        validate_schema(
            "job.submitted",
            {
                "event": "job.submitted",
                # job_id missing
                "user_id": str(uuid.uuid4()),
                "job_type": "csv_upload",
            },
        )


def test_invalid_uuid_format_raises() -> None:
    with pytest.raises(SchemaValidationError):
        validate_schema(
            "job.submitted",
            {
                "event": "job.submitted",
                "job_id": "not-a-uuid",
                "user_id": str(uuid.uuid4()),
                "job_type": "csv_upload",
            },
        )


def test_wrong_event_const_raises() -> None:
    with pytest.raises(SchemaValidationError):
        validate_schema(
            "job.submitted",
            {
                "event": "wrong",
                "job_id": str(uuid.uuid4()),
                "user_id": str(uuid.uuid4()),
                "job_type": "csv_upload",
            },
        )


def test_progress_percent_out_of_range_raises() -> None:
    with pytest.raises(SchemaValidationError):
        validate_schema(
            "job.progress",
            {
                "event": "job.progress",
                "job_id": str(uuid.uuid4()),
                "user_id": str(uuid.uuid4()),
                "status": "running",
                "percent": 150,  # > 100
            },
        )


def test_dlq_uses_same_shape_as_failed() -> None:
    """job.dlq is registered with the job_failed schema."""
    validate_schema(
        "job.dlq",
        {
            "event": "job.failed",
            "tenant_id": str(uuid.uuid4()),
            "job_id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
            "job_type": "csv_upload",
            "error": "boom",
            "retry_count": 3,
            "dead_lettered": True,
        },
    )


def _dlq_base() -> dict[str, object]:
    return {
        "event": "job.failed",
        "tenant_id": str(uuid.uuid4()),
        "job_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "job_type": "csv_upload",
        "error": "boom",
        "retry_count": 3,
        "dead_lettered": True,
    }


def test_dlq_with_triage_context_validates() -> None:
    """E1-14: `job.dlq` now carries max_retries / payload / trace_id."""
    validate_schema(
        "job.dlq",
        {**_dlq_base(), "max_retries": 3, "payload": {"file": "x.csv"}, "trace_id": "t-1"},
    )


def test_dlq_with_truncated_payload_marker_validates() -> None:
    validate_schema(
        "job.dlq",
        {
            **_dlq_base(),
            "max_retries": 3,
            "payload": {"_truncated": True, "_original_bytes": 9001},
            "trace_id": None,
        },
    )


def test_failed_without_triage_context_still_validates() -> None:
    """The retry-path `job.failed` events share this schema and carry none of
    the three fields — they must stay optional or every retry event would be
    dropped by `publish_raw`'s validation."""
    validate_schema("job.failed", {**_dlq_base(), "dead_lettered": False})


def test_dlq_rejects_malformed_triage_context() -> None:
    """The new fields are typed, so producer-side validation catches drift
    before a malformed event reaches triage."""
    with pytest.raises(SchemaValidationError):
        validate_schema("job.dlq", {**_dlq_base(), "max_retries": -1})
    with pytest.raises(SchemaValidationError):
        validate_schema("job.dlq", {**_dlq_base(), "payload": "not-an-object"})
    with pytest.raises(SchemaValidationError):
        validate_schema("job.dlq", {**_dlq_base(), "trace_id": 42})


def test_unknown_topic_raises_instead_of_passing_silently() -> None:
    """An unmapped topic must fail loudly, not report success by doing nothing.

    This assertion is inverted from what it used to be. The old behaviour —
    return None for any topic with no schema — meant that the one case the
    registry exists to catch (a topic shipped before its schema) was the one
    case it waved through, while every call site logged it as validated.
    """
    with pytest.raises(UnknownTopicError):
        validate_schema("some.unknown.topic", {"anything": "goes"})


def test_unknown_topic_error_is_catchable_as_a_schema_failure() -> None:
    """Producer and consumer both catch SchemaValidationError already.

    An unmapped topic has to travel that same path — publish is skipped, a
    consumed message is committed past — or adding a topic without a schema
    would crash the worker loop instead of degrading it.
    """
    assert issubclass(UnknownTopicError, SchemaValidationError)


def test_every_configured_topic_has_a_schema() -> None:
    """The CLAUDE.md rule, as a test: every `Settings.kafka_topic_*` is covered.

    Asserting on the derived mapping rather than a second hand-written list —
    a list here could drift from the one in the registry exactly as the
    registry's drifted from Settings.
    """
    settings = get_settings()
    topics = {
        str(getattr(settings, field))
        for field in type(settings).model_fields
        if field.startswith("kafka_topic_")
    }
    assert topics, "no kafka_topic_* fields found — the sweep is broken"

    mapping = topic_schema_files()
    assert set(mapping) == topics

    schema_dir = Path(schema_registry.__file__).resolve().parent.parent / "schemas" / "kafka"
    missing = sorted(f for f in mapping.values() if not (schema_dir / f).is_file())
    assert not missing, f"topics configured with no schema file: {missing}"


def test_a_new_topic_without_a_schema_fails_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate itself, exercised: add a topic, get a boot failure.

    Adding a real field to Settings would leak into every other test in the
    session, so the field is added to a throwaway subclass and `get_settings`
    is pointed at it for the duration.
    """
    settings = get_settings()

    class SettingsWithNewTopic(type(settings)):  # type: ignore[misc,valid-type]
        kafka_topic_job_archived: str = "job.archived"

    monkeypatch.setattr(
        schema_registry, "get_settings", lambda: SettingsWithNewTopic()  # type: ignore[call-arg]
    )

    assert "job.archived" in schema_registry.topic_schema_files()
    with pytest.raises(schema_registry.SchemaRegistryError) as excinfo:
        schema_registry.reload()

    assert "job.archived" in str(excinfo.value)
    assert "job_archived.schema.json" in str(excinfo.value)
