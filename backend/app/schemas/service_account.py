import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ServiceAccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    scopes: list[str] = Field(
        default_factory=list,
        description="Machine scopes granted to this account. Minted tokens "
        "must carry a subset of this list.",
    )


class ServiceAccountScopesUpdate(BaseModel):
    """PATCH payload — replaces the SA's scope set. Existing tokens
    keep the scope subset they were minted with; fresh tokens use the
    updated set."""

    scopes: list[str] = Field(
        min_length=0,
        description="Full replacement scope list. Pass every scope the "
        "account should have; anything omitted is dropped.",
    )


class ServiceAccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    scopes: list[str]
    is_active: bool
    created_by_user_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class TokenMintRequest(BaseModel):
    scopes: list[str] | None = Field(
        default=None,
        description="Subset of the account's scopes to mint into this token. "
        "Omit to inherit the full account scope set.",
    )
    ttl_days: int | None = Field(
        default=None,
        ge=1,
        le=365,
        description="Time-to-live in days. Omit for the platform default (90).",
    )


class TokenResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    service_account_id: uuid.UUID
    scopes: list[str]
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime


class TokenMintResponse(BaseModel):
    """Returned once, at mint time. `plaintext` is not persisted anywhere and
    can never be retrieved again — the operator must capture it now."""

    token: TokenResponse
    plaintext: str = Field(
        description="Bearer token to present as `Authorization: Bearer <token>`. "
        "Shown only at mint time; not recoverable."
    )
