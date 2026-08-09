"""Unit tests for the JSON Schema registry."""

import uuid

import pytest
from app.workers.schema_registry import SchemaValidationError
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


def test_unknown_topic_is_noop() -> None:
    """Validating against an unregistered topic must not raise — keeps callers
    simple when new topics appear before their schemas are written."""
    validate_schema("some.unknown.topic", {"anything": "goes"})
