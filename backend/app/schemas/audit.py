import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.schemas.common import PaginationParams


class AuditLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID | None
    job_id: uuid.UUID | None
    action: str
    resource_type: str | None
    resource_id: str | None
    request_id: str | None
    ip_address: str | None
    extra_data: dict[str, Any] | None
    created_at: datetime


class AuditListParams(PaginationParams):
    user_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    action: str | None = None
