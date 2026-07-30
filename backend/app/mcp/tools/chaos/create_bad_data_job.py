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


from app.core.logging import get_logger
from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext
from app.models.enums import JobStatus, JobType, RemediationHint
from app.models.job import Job
from app.models.tenant import DEFAULT_TENANT_ID
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

    # Grab any active user in the caller's tenant to satisfy the Job
    # FK. Falls back to the default tenant's first user for local
    # dev where a tenant may only have the seed accounts.
    tenant_id = ctx.principal.tenant_id
    user = (
        await ctx.db.execute(
            select(User).where(User.tenant_id == tenant_id).limit(1)
        )
    ).scalar_one_or_none()
    if user is None and tenant_id != DEFAULT_TENANT_ID:
        user = (
            await ctx.db.execute(
                select(User).where(User.tenant_id == DEFAULT_TENANT_ID).limit(1)
            )
        ).scalar_one_or_none()

    if user is None:
        # No user to attach the job to — chaos degrades gracefully.
        return CreateBadDataJobOutput(
            job_id="",
            remediation_hint=RemediationHint.HUMAN_REQUIRED.value,
            accepted=False,
        )

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
