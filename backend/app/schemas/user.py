import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    # No `role` field: the public register body must never carry a role.
    # Everyone registers as a plain user; the only elevation is the
    # founder-of-a-new-tenant branch inside AuthService.register, which
    # does not read the request body (X-01 / F1-04).
    #
    # `tenant_slug` IS still free-form, but since WO-R2-25 it only selects
    # between two outcomes the service is willing to grant unauthenticated:
    # a brand-new tenant (with `new_tenant_name`) or the shared default one.
    # Naming any other existing tenant is refused with 403 — see ADR 0024.
    email: EmailStr
    password: str = Field(min_length=8)
    tenant_slug: str = Field(default="default", min_length=1, max_length=64)
    # When set, the slug is created on the fly if it doesn't exist and the
    # registering user becomes its admin. Used by the "start a new
    # workspace" path on the register page.
    new_tenant_name: str | None = Field(default=None, min_length=1, max_length=128)


class TenantMemberCreate(BaseModel):
    """Body for an admin enrolling someone into their own tenant.

    Deliberately has no `tenant_slug` and no `role`. Both are taken from the
    authenticated admin instead of the request — a tenant identifier the
    caller gets to choose is the whole of WO-R2-25, and a role the caller
    gets to choose is X-01. The absent fields are the feature.
    """

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
