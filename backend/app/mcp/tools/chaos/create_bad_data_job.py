"""
`create_bad_data_job` — inject a realistic `human_required` DLQ entry.

The persistent-bug counterpart to `poison_message` (which produces
`replay_safe` entries). The agent needs a way to reach the
"escalate, do NOT replay" branch of its remediation loop against a
live platform; this hook drops a synthetic DLQ row that the
`replay_dlq_by_category` guardrail will refuse.

Doesn't touch Kafka — writes directly to `jobs` with
`status=dead_letter`, `remediation_hint=human_required`, and a
realistic error string. Chaos-only surface: gated behind
`CHAOS_ENABLED=true` + `chaos:invoke` scope + `environment_wide`
blast radius label.
"""


import uuid

from app.core.logging import get_logger
from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext
from app.models.enums import JobStatus, JobType, RemediationHint, UserRole
from app.models.job import Job
from app.models.user import User
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select


class CreateBadDataJobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_type: str = Field(
        default=JobType.CSV_UPLOAD.value,
        description=(
            "Type stamped on the synthetic job. Should match a real "
            "processor type so the agent's downstream reasoning stays "
            "plausible."
        ),
    )
    error_message: str = Field(
        default=(
            "ValueError: invalid literal for int() with base 10: "
            "'not-a-number' at row 15,382"
        ),
        max_length=2048,
        description="Realistic error string. Default matches the same "
        "shape as the seed fixture's persistent-bug entry.",
    )


class CreateBadDataJobOutput(BaseModel):
    job_id: str
    remediation_hint: str
    accepted: bool


@chaos_tool(
    "create_bad_data_job",
    description=(
        "Inject a synthetic DLQ entry with "
        "`remediation_hint=human_required`. Complement to "
        "`poison_message` (which produces `replay_safe` entries) — "
        "gives the agent a persistent-bug case to reach the "
        "escalate-not-replay branch. Written directly to jobs; "
        "doesn't touch Kafka."
    ),
    input_model=CreateBadDataJobInput,
    output_model=CreateBadDataJobOutput,
    blast_radius=BlastRadius.ENVIRONMENT_WIDE,
)
async def create_bad_data_job(
    inp: CreateBadDataJobInput, ctx: ToolContext
) -> CreateBadDataJobOutput:
    logger = get_logger(__name__)
    tenant_id = ctx.principal.tenant_id

    # Prefer any real user in the caller's tenant to satisfy the Job
    # FK. When the tenant is unseeded (common in local dev / fresh
    # eval env), lazy-create a chaos-owned user in the SAME tenant.
    # The pre-v0.4.6 shape fell back to a user from DEFAULT_TENANT_ID,
    # violating the tenant-isolation invariant (jobs.tenant_id and
    # users.tenant_id ended up pointing at different tenants). See
    # ADR 0003 (RLS as defense-in-depth).
    user = (
        await ctx.db.execute(
            select(User).where(User.tenant_id == tenant_id).limit(1)
        )
    ).scalar_one_or_none()
    if user is None:
        user = await _ensure_chaos_owner(ctx, tenant_id)

    job = Job(
        tenant_id=tenant_id,
        user_id=user.id,
        type=inp.job_type,
        status=JobStatus.DEAD_LETTER.value,
        payload={"chaos_fixture": "bad_data_job"},
        retry_count=3,
        error_message=inp.error_message,
        remediation_hint=RemediationHint.HUMAN_REQUIRED.value,
    )
    ctx.db.add(job)
    await ctx.db.flush()

    logger.warning(
        "chaos create_bad_data_job injected",
        extra={
            "job_id": str(job.id),
            "tenant_id": str(tenant_id),
            "job_type": inp.job_type,
        },
    )
    return CreateBadDataJobOutput(
        job_id=str(job.id),
        remediation_hint=RemediationHint.HUMAN_REQUIRED.value,
        accepted=True,
    )


# Chaos-created users use a deterministic email + a well-known unusable
# password so a login attempt against them fails cleanly (bcrypt refuses
# to parse "!") and cleanup scripts can grep them by prefix. `is_active`
# is False so any endpoint that filters active users doesn't surface
# them in operator-facing lists.
_CHAOS_OWNER_EMAIL_PREFIX = "chaos-owner"
_CHAOS_UNUSABLE_PASSWORD = "!chaos-owner-no-login"


async def _ensure_chaos_owner(ctx: ToolContext, tenant_id: uuid.UUID) -> User:
    """Get-or-create a chaos-owned user in `tenant_id`. Idempotent —
    the tenant-scoped email means repeat calls in the same tenant
    return the same row."""
    email = f"{_CHAOS_OWNER_EMAIL_PREFIX}+{tenant_id}@chaos.local"
    existing = (
        await ctx.db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    user = User(
        tenant_id=tenant_id,
        email=email,
        hashed_password=_CHAOS_UNUSABLE_PASSWORD,
        role=UserRole.USER.value,
        is_active=False,
    )
    ctx.db.add(user)
    await ctx.db.flush()
    return user
