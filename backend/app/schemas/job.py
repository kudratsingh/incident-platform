import uuid
from datetime import datetime
from typing import Any

from app.models.enums import JobStatus, JobType
from app.schemas.common import PaginationParams
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# ---------------------------------------------------------------------------
# Per-type payload bounds
#
# The payload is user-controlled and its knobs translate directly into work in
# the worker process — which is the SAME process that serves the API and hosts
# every consumer loop. endpoint_count=10**7 eagerly creates 10**7 asyncio tasks
# and OOM-kills that process; page_count/row_count peg the 4-worker process
# pool long enough to push the Kafka dispatcher past max_poll_interval_ms and
# get the whole consumer group kicked.
#
# extra="allow" is required, not incidental: payloads legitimately carry
# __traceparent (injected in JobService.create_job) plus arbitrary
# caller-defined keys. Only the knobs the processors actually read are bounded.
#
# These caps are deliberately conservative and defensive — the Phase 14 "real"
# processors will want their own, likely lower, resource-derived limits.
# ---------------------------------------------------------------------------

_PAYLOAD_BOUNDS = ConfigDict(extra="allow")


class BulkApiSyncPayload(BaseModel):
    model_config = _PAYLOAD_BOUNDS

    endpoint_count: int = Field(default=5, ge=0, le=100)


class CsvUploadPayload(BaseModel):
    model_config = _PAYLOAD_BOUNDS

    row_count: int = Field(default=500, ge=0, le=1_000_000)
    # ge=1, not ge=0: chunk_size is a divisor in process_csv_upload.
    chunk_size: int = Field(default=100, ge=1, le=100_000)


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


def validate_processor_payload(job_type: str, payload: dict[str, Any] | None) -> None:
    """Raise ValueError if `payload` exceeds the bounds for `job_type`.

    Shared by every creation surface: POST /jobs (JobCreate) and POST /sagas
    (SagaStepRequest), which does NOT go through JobCreate. Bounding only one
    of them leaves the other as a trivial bypass.

    Job types with no bound model — saga compensation types like
    `csv_upload.compensate`, or anything not in JobType — are a no-op.

    Raises ValueError rather than re-raising the pydantic ValidationError so
    that a caller-side @model_validator turns it into the standard flat 422
    envelope instead of a doubly-nested error body.
    """
    if payload is None:
        return
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
    max_retries: int
    # Attribution for a DLQ row (F2-16). REST-only on purpose: the MCP tool
    # output models are contract-frozen, and the admin UI is the only
    # consumer that needs it.
    dead_lettered_by: str | None = None
    priority: int
    trace_id: str | None
    saga_id: uuid.UUID | None = None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class StreamTokenResponse(BaseModel):
    """A short-lived, single-purpose token for GET /jobs/{id}/stream?token=…

    Minted by POST /jobs/{id}/stream-token after the caller was authorized
    for the job. Expires in STREAM_TOKEN_TTL_SECONDS (see ADR 0014).
    """

    token: str


class JobListParams(PaginationParams):
    status: JobStatus | None = None
    type: JobType | None = None
    trace_id: str | None = None


class AdminJobListParams(JobListParams):
    user_id: uuid.UUID | None = None
