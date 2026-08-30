"""Request bodies for the tenant admin surface."""

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

#: Width of the `INTEGER` columns these limits are stored in. A wider
#: value is not a policy question — Postgres cannot hold it, so an
#: unbounded field turns a caller's typo into a 500 from the UPDATE.
INT32_MAX = 2**31 - 1


class TenantLimitsUpdate(BaseModel):
    """Partial update of a tenant's rate limit and monthly job quota.

    Both fields are optional; only the ones present in the body are
    applied, which is why the handler reads `model_fields_set` rather
    than the model's defaults.

    `StrictInt` rather than `int` is the load-bearing part. This endpoint
    used to take an untyped `dict` and validate with
    `isinstance(value, int)` — and in Python `isinstance(True, int)` is
    `True`, because `bool` subclasses `int`. A JSON `true` therefore
    passed the guard and was written as a rate limit of **1**, throttling
    a whole tenant to one request a minute with a 200 OK in reply
    (WO-R2-61). Pydantic's lax mode has the same hole (it coerces `True`
    to `1`); strict mode is what closes it, and it also refuses the
    numeric strings and floats the old check happened to reject by
    accident rather than by design.
    """

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
        """Reject an explicit `null`, while leaving an omitted field alone.

        A field validator does not run for a field the caller omitted, so
        reaching this with `None` means the body really did carry
        `"rate_limit_per_minute": null`. Both columns are NOT NULL, so
        that is a client bug; answering 200 and changing nothing would
        hide it. The `| None` in the annotation is the absent-field
        sentinel only.
        """
        if value is None:
            raise ValueError(
                "must be a non-negative integer; null does not clear a limit"
            )
        return value
