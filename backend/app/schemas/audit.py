import uuid
from datetime import datetime
from typing import Any, Literal

from app.schemas.common import PaginationParams
from pydantic import BaseModel, ConfigDict, Field


class AuditLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID | None
    # Canonical actor identity going forward. `user_id` remains populated
    # for human actors so existing clients don't break.
    principal_type: str = "user"
    principal_id: uuid.UUID | None = None
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
    # `action` is an exact match, which cannot isolate a stream: `agent.`,
    # `chaos.` and `job.` are prefixes, and the console's audit views filter on
    # whole streams. Bounded because it reaches a LIKE pattern.
    action_prefix: str | None = Field(default=None, max_length=100)
    # Isolate operator activity (`user`) from agent activity
    # (`service_account`). Omit to see both.
    principal_type: Literal["user", "service_account"] | None = None
