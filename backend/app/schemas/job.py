import json
import uuid
from datetime import datetime
from typing import Any

from app.config import get_settings
from app.models.enums import JobStatus, JobType
from app.schemas.common import PaginationParams
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    computed_field,
    model_validator,
)

# Per-type payload bounds: the payload is user-controlled and its knobs become work in
# the worker process that also serves the API, so an unbounded one OOMs or stalls it.
# extra="allow" is required — payloads carry __traceparent plus caller-defined keys, and
# only the knobs the processors actually read are bounded.

_PAYLOAD_BOUNDS = ConfigDict(extra="allow")


class BulkApiSyncPayload(BaseModel):
    model_config = _PAYLOAD_BOUNDS

    endpoint_count: int = Field(default=5, ge=0, le=100)


# Largest number of chunks a csv_upload may be split into (WO-R2-07). The work is the
# chunk COUNT — `ceil(row_count / chunk_size)` — so bounding the two fields separately
# let `{row_count: 1_000_000, chunk_size: 1}` buy hours of execution. Bound the
# quotient, not the product. 10_000 is the documented max `row_count` at the default
# `chunk_size`, so the worst accepted job stays inside `job_execution_timeout_seconds`.
MAX_CSV_CHUNKS = 10_000


class CsvUploadPayload(BaseModel):
    model_config = _PAYLOAD_BOUNDS

    row_count: int = Field(default=500, ge=0, le=1_000_000)
    # ge=1, not ge=0: chunk_size is a divisor in process_csv_upload.
    chunk_size: int = Field(default=100, ge=1, le=100_000)

    @model_validator(mode="after")
    def _bound_chunk_count(self) -> "CsvUploadPayload":
        chunks = -(-self.row_count // self.chunk_size)  # ceil, no float math
        if chunks > MAX_CSV_CHUNKS:
            raise ValueError(
                f"row_count / chunk_size yields {chunks} chunks, which exceeds "
                f"the {MAX_CSV_CHUNKS}-chunk limit; raise chunk_size"
            )
        return self


class DocAnalysisPayload(BaseModel):
    model_config = _PAYLOAD_BOUNDS

    page_count: int = Field(default=10, ge=0, le=1000)


class ReportGenPayload(BaseModel):
    model_config = _PAYLOAD_BOUNDS

    row_count: int = Field(default=10_000, ge=0, le=1_000_000)
    # ge=1, not ge=0: group_count is a divisor in _generate_report.
    group_count: int = Field(default=10, ge=1, le=1000)


_PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    JobType.BULK_API_SYNC.value: BulkApiSyncPayload,
    JobType.CSV_UPLOAD.value: CsvUploadPayload,
    JobType.DOC_ANALYSIS.value: DocAnalysisPayload,
    JobType.REPORT_GEN.value: ReportGenPayload,
}


def _bound_payload_size(payload: dict[str, Any]) -> None:
    """Reject a payload too large to ever survive the trip to Kafka.

    `extra="allow"` bounds only the knobs the processors read, so one arbitrary key can push
    the outbox row past the broker's `message.max.bytes` — a poison row any user can create.
    Measured on the serialised form, since bytes on the wire are what the broker counts.
    """
    size = len(json.dumps(payload, default=str).encode())
    limit = get_settings().max_job_payload_bytes
    if size > limit:
        raise ValueError(
            f"payload is {size} bytes, which exceeds the {limit}-byte limit"
        )


def validate_processor_payload(job_type: str, payload: dict[str, Any] | None) -> None:
    """Raise ValueError if `payload` exceeds the bounds for `job_type`.

    Shared by POST /jobs and POST /sagas — bounding one leaves the other a bypass; a type
    with no bound model still gets the size check. ValueError keeps the flat 422 envelope.
    """
    if payload is None:
        return
    _bound_payload_size(payload)
    model = _PAYLOAD_MODELS.get(str(job_type))
    if model is None:
        return
    try:
        model.model_validate(payload)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'payload'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ValueError(f"invalid payload for job type '{job_type}': {details}") from None


class JobCreate(BaseModel):
    type: JobType
    payload: dict[str, Any] | None = None
    idempotency_key: str | None = Field(default=None, max_length=255)
    priority: int = Field(default=0, ge=0, le=100)
    dependencies: list[uuid.UUID] = Field(
        default_factory=list,
        description="Parent job IDs that must reach COMPLETED before this one runs.",
    )

    @model_validator(mode="after")
    def _bound_payload(self) -> "JobCreate":
        validate_processor_payload(self.type.value, self.payload)
        return self


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    type: str
    status: str
    idempotency_key: str | None
    payload: dict[str, Any] | None
    result: dict[str, Any] | None
    error_message: str | None
    retry_count: int
    # Total runs this job may have — the original plus its retries. 3 means
    # three runs and two retries (WO-R2-172).
    max_attempts: int
    # DLQ attribution (F2-16). REST-only — the MCP output models are frozen.
    dead_lettered_by: str | None = None
    priority: int
    trace_id: str | None
    saga_id: uuid.UUID | None = None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    @computed_field(  # type: ignore[prop-decorator]
        description=(
            "DEPRECATED — use max_attempts, which carries the identical "
            "value. This field never held a retry count: it has always "
            "capped total runs. Kept for one release so clients can "
            "migrate; removed after that."
        ),
        # In the schema, not `deprecated=`: that would warn this process
        # about a field choice only the HTTP client can act on.
        json_schema_extra={"deprecated": True},
    )
    @property
    def max_retries(self) -> int:
        """The old name for `max_attempts` (WO-R2-172); computed so there is exactly one number."""
        return self.max_attempts


class StreamTokenResponse(BaseModel):
    """Short-lived job-bound token for GET /jobs/{id}/stream?token=… (ADR 0014)."""

    token: str


class JobListParams(PaginationParams):
    status: JobStatus | None = None
    type: JobType | None = None
    trace_id: str | None = None


class AdminJobListParams(JobListParams):
    user_id: uuid.UUID | None = None
