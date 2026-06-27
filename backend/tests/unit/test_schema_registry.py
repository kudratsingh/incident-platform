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
            "job_id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
            "job_type": "csv_upload",
            "error": "boom",
            "retry_count": 3,
            "dead_lettered": True,
        },
    )


def test_unknown_topic_is_noop() -> None:
    """Validating against an unregistered topic must not raise — keeps callers
    simple when new topics appear before their schemas are written."""
    validate_schema("some.unknown.topic", {"anything": "goes"})
