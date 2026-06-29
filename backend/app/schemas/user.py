import uuid
from datetime import datetime

from app.models.enums import UserRole
from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    role: UserRole = UserRole.USER
    tenant_slug: str = Field(default="default", min_length=1, max_length=64)
    # When set, the slug is created on the fly if it doesn't exist and the
    # registering user becomes its admin. Used by the "start a new
    # workspace" path on the register page.
    new_tenant_name: str | None = Field(default=None, min_length=1, max_length=128)


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
