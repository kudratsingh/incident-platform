"""Request bodies for the tenant admin surface."""

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

#: Width of the `INTEGER` columns these limits are stored in. A wider
#: value is not a policy question — Postgres cannot hold it, so an
#: unbounded field turns a caller's typo into a 500 from the UPDATE.
INT32_MAX = 2**31 - 1


class TenantLimitsUpdate(BaseModel):
    """Partial update of a tenant's rate limit and quota; `StrictInt` refuses bool (WO-R2-61)."""

    model_config = ConfigDict(extra="forbid")

    rate_limit_per_minute: StrictInt | None = Field(
        default=None,
        ge=0,
        le=INT32_MAX,
        description="Requests per minute per tenant. 0 disables the check.",
    )
    quota_jobs_per_month: StrictInt | None = Field(
        default=None,
        ge=0,
        le=INT32_MAX,
        description="Job admissions per calendar month. 0 disables the check.",
    )

    @field_validator("rate_limit_per_minute", "quota_jobs_per_month")
    @classmethod
    def _null_does_not_clear_a_limit(cls, value: int | None) -> int | None:
        """Reject an explicit `null`; an omitted field never reaches a field validator.

        Both columns are NOT NULL, so `null` is a client bug — 200-and-do-nothing hides it.
        """
        if value is None:
            raise ValueError(
                "must be a non-negative integer; null does not clear a limit"
            )
        return value
