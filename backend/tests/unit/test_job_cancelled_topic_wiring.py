"""`job.cancelled` is wired end to end: setting, schema, and four consumers.

CANCELLED was the only terminal status with no lifecycle event (WO-R2-113).
The consequence was not that "an event was missing" in the abstract — it was
that every consumer of the terminal lifecycle went on believing the previous
state forever: the CQRS read model kept the id in its `running` set, the event
log had no row to show on the timeline, the SSE stream never closed, and the
audit trail recorded a job that simply stopped.

These tests pin the wiring rather than the behaviour (each consumer's own
suite pins that): a topic nobody subscribes to is exactly as silent as no
topic at all, and the subscription list is the one part of the chain that no
behavioural test exercises — `handle_message` is always called directly.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from app.config import get_settings
from app.models.enums import TERMINAL_JOB_STATUSES, JobStatus
from app.workers import schema_registry
from app.workers.audit_consumer import AuditConsumer
from app.workers.event_log_consumer import EventLogConsumer
from app.workers.read_model import ReadModelProjector
from app.workers.sse_consumer import SseConsumer


def _cancelled_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "event": "job.cancelled",
        "tenant_id": "11111111-1111-4111-8111-111111111111",
        "job_id": "22222222-2222-4222-8222-222222222222",
        "user_id": "33333333-3333-4333-8333-333333333333",
        "job_type": "csv_upload",
        "reason": "saga rollback",
        "retry_count": 0,
    }
    payload.update(overrides)
    return payload


def test_settings_declare_the_cancelled_topic() -> None:
    assert get_settings().kafka_topic_job_cancelled == "job.cancelled"


def test_a_valid_cancelled_payload_passes_the_registry() -> None:
    """The producer half of the contract, checked the way `publish_raw` does."""
    schema_registry.validate("job.cancelled", _cancelled_payload())


def test_the_cancelled_schema_allows_additional_properties() -> None:
    """docs/KAFKA.md's evolution rule: every schema is open, so a producer can
    ship a new field before any consumer reads it. A closed schema here would
    make the next additive change a breaking one."""
    path = (
        Path(schema_registry.__file__).resolve().parent.parent
        / "schemas"
        / "kafka"
        / "job_cancelled.schema.json"
    )
    schema = json.loads(path.read_text())
    assert schema["additionalProperties"] is True
    assert schema["$id"] == "job.cancelled/v1"


@pytest.mark.parametrize(
    "missing", ["event", "tenant_id", "job_id", "user_id", "job_type", "reason"]
)
def test_the_required_fields_are_required(missing: str) -> None:
    """Each field, removed on its own from an otherwise complete payload.

    Matched on the field name rather than on the exception type, to the
    standard #191 set: `validate` raises `UnknownTopicError` — a
    `SchemaValidationError` subclass — for a topic with no schema at all, so
    a bare `pytest.raises` here passed before the topic existed and would go
    on passing if the schema file were deleted tomorrow. The name in the
    message is what ties the assertion to the field it claims to be about.
    """
    payload = _cancelled_payload()
    del payload[missing]
    with pytest.raises(schema_registry.SchemaValidationError, match=missing):
        schema_registry.validate("job.cancelled", payload)


def test_tenant_id_is_required_because_two_consumers_drop_events_without_it() -> None:
    """`EventLogConsumer` and `AuditConsumer` both skip a payload with no
    tenant_id — silently, in the first case. Making it required in the schema
    turns that into a producer-side failure instead.

    A complete payload with only `tenant_id` malformed, so deleting
    `format: uuid` from the schema is what turns this red — not a second
    missing field standing in for the check.
    """
    with pytest.raises(schema_registry.SchemaValidationError, match="nope"):
        schema_registry.validate("job.cancelled", _cancelled_payload(tenant_id="nope"))


def test_all_four_lifecycle_consumers_subscribe_to_the_cancelled_topic() -> None:
    """A schema and an outbox row prove nothing if no consumer group reads the
    partition. All four lifecycle consumers must be on it."""
    topic = get_settings().kafka_topic_job_cancelled
    consumers = {
        "read-model": ReadModelProjector(AsyncMock()),
        "sse": SseConsumer(AsyncMock()),
        "event-log": EventLogConsumer(AsyncMock()),
        "audit": AuditConsumer(AsyncMock()),
    }
    missing = [name for name, c in consumers.items() if topic not in c.topics]
    assert missing == [], f"consumers not subscribed to {topic}: {missing}"


def test_every_terminal_status_now_has_an_announcing_topic() -> None:
    """The generalisation of this work order. Written against
    `TERMINAL_JOB_STATUSES` rather than a hand-listed set so that the next
    terminal status added to the enum fails here until it has a topic —
    which is the mistake CANCELLED represented for the platform's whole life.
    """
    from app.repositories.job import _TERMINAL_EVENT_STATUSES

    assert set(TERMINAL_JOB_STATUSES) == set(_TERMINAL_EVENT_STATUSES)
    assert JobStatus.CANCELLED in _TERMINAL_EVENT_STATUSES
