import uuid
from datetime import datetime
from typing import Any

from app.models.enums import JobStatus, JobType
from app.schemas.common import PaginationParams
from pydantic import BaseModel, ConfigDict, Field


class JobCreate(BaseModel):
    type: JobType
    payload: dict[str, Any] | None = None
    idempotency_key: str | None = Field(default=None, max_length=255)
    priority: int = Field(default=0, ge=0, le=100)


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
    priority: int
    trace_id: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class JobListParams(PaginationParams):
    status: JobStatus | None = None
    type: JobType | None = None
    trace_id: str | None = None


class AdminJobListParams(JobListParams):
    user_id: uuid.UUID | None = None
