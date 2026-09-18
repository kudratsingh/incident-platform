import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    # No `role` field: the register body must never carry one (X-01 / F1-04).
    # `tenant_slug` picks only a new tenant or the shared default; naming any other
    # existing one is 403 (WO-R2-25, ADR 0024).
    email: EmailStr
    password: str = Field(min_length=8)
    tenant_slug: str = Field(default="default", min_length=1, max_length=64)
    # When set, the slug is created on the fly and the registrant becomes its admin.
    new_tenant_name: str | None = Field(default=None, min_length=1, max_length=128)


class TenantMemberCreate(BaseModel):
    """Admin enrols a user into their own tenant; `role`/`tenant_slug` come from the token."""

    email: EmailStr
    password: str = Field(min_length=8)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    tenant_slug: str | None = None
    email: str
    role: str
    is_active: bool
    is_platform_admin: bool = False
    created_at: datetime
