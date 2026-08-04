"""
`seed_dlq_messages` — create N DLQ rows with declared types and hints.

Platform half of commander ADR 0010 (scenario-owned DLQ fixtures). The
standing 4-row fixture pool was an attractive nuisance: always present,
always plausible, never the scenario's subject. Three campaign runs in
one night pivoted onto it when their real subject was absent. The fix
is that a scenario needing DLQ content *declares* it, the same way
chaos faults are already declared.

Why this lives under chaos rather than as a plain seed helper: it
writes `dead_letter` rows into a live database. That is fault
injection whatever we call it, so it inherits the triple gate from
[ADR 0008](../../../../docs/ADR/0008-chaos-gating.md) —
`CHAOS_ENABLED` + `chaos:invoke` + blast-radius check — and can
therefore never fire in production.

Rows are tagged `payload.seeded_fixture = true` so the reset sweep can
DELETE them rather than cancel them. Chaos rows created by
`create_bad_data_job` are *cancelled* on sweep because they may be
attached to a real user and read as that user's history; these are
explicitly ephemeral scaffolding declared by a scenario, so leaving
thousands of `cancelled` rows behind across eval runs would be litter,
not history.
"""

import uuid

from app.core.logging import get_logger
from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext
from app.models.enums import JobStatus, JobType, RemediationHint
from app.models.job import Job
from app.models.user import User
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

logger = get_logger(__name__)

# Marker the reset sweep keys off. Distinct from `chaos_fixture`
# (create_bad_data_job / poison_message) because the disposal rule
# differs — see module docstring.
SEEDED_FIXTURE_MARKER = "seeded_fixture"

# Canned error strings per hint, so a scenario that only declares
# `remediation_hint` still gets a string the agent's triage can read as
# realistic. Lifted from the retired `_dlq_specs()` pool, which is what
# these defaults replace.
_DEFAULT_ERRORS: dict[str, str] = {
    RemediationHint.REPLAY_SAFE.value: (
        "SchemaValidationError: payload missing required field "
        "'user_id' (received keys: ['tenant_id', 'action', 'ts'])"
    ),
    RemediationHint.WAIT_AND_REPLAY.value: (
        "send_email downstream call failed: "
        "ConnectionRefusedError('smtp.mailer.internal:587')"
    ),
    RemediationHint.HUMAN_REQUIRED.value: (
        "ValueError: invalid literal for int() with base 10: "
        "'not-a-number' at row 15,382"
    ),
}


class SeedDlqMessagesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remediation_hint: str = Field(
        description=(
            "Category stamped on every row: `replay_safe`, "
            "`wait_and_replay`, or `human_required`. Drives which "
            "branch of the agent's remediation logic the rows exercise."
        )
    )
    count: int = Field(
        default=1,
        ge=1,
        le=50,
        description="How many rows to create. Capped so a scenario "
        "can't accidentally flood the DLQ.",
    )
    job_type: str = Field(
        default=JobType.BULK_API_SYNC.value,
        description="Type stamped on each row. Should match a real "
        "processor type so downstream reasoning stays plausible.",
    )
    error_message: str | None = Field(
        default=None,
        max_length=2048,
        description="Error string for every row. Defaults to a "
        "realistic string matching the declared remediation_hint.",
    )


class SeedDlqMessagesOutput(BaseModel):
    job_ids: list[str]
    remediation_hint: str
    count: int
    accepted: bool


@chaos_tool(
    "seed_dlq_messages",
    description=(
        "Seed N dead-letter rows with a declared `remediation_hint`, "
        "`job_type`, and error string. Lets a scenario own the DLQ "
        "state it is graded against instead of inheriting a standing "
        "fixture pool. Rows are tagged as seeded fixtures and removed "
        "by the next eval reset. Written directly to `jobs`; doesn't "
        "touch Kafka."
    ),
    input_model=SeedDlqMessagesInput,
    output_model=SeedDlqMessagesOutput,
    blast_radius=BlastRadius.ENVIRONMENT_WIDE,
)
async def seed_dlq_messages(
    inp: SeedDlqMessagesInput, ctx: ToolContext
) -> SeedDlqMessagesOutput:
    hint = _validated_hint(inp.remediation_hint)
    tenant_id = ctx.principal.tenant_id
    user = await _fixture_owner(ctx, tenant_id)
    error_message = inp.error_message or _DEFAULT_ERRORS[hint]

    jobs: list[Job] = []
    for _ in range(inp.count):
        job = Job(
            tenant_id=tenant_id,
            user_id=user.id,
            type=inp.job_type,
            status=JobStatus.DEAD_LETTER.value,
            payload={SEEDED_FIXTURE_MARKER: True},
            retry_count=3,
            error_message=error_message,
            remediation_hint=hint,
        )
        ctx.db.add(job)
        jobs.append(job)
    # Ids are assigned on flush — collecting them before this point
    # yields "None" strings.
    await ctx.db.flush()
    job_ids = [str(j.id) for j in jobs]

    logger.warning(
        "chaos seed_dlq_messages injected",
        extra={
            "tenant_id": str(tenant_id),
            "remediation_hint": hint,
            "count": inp.count,
            "job_type": inp.job_type,
        },
    )
    return SeedDlqMessagesOutput(
        job_ids=job_ids,
        remediation_hint=hint,
        count=inp.count,
        accepted=True,
    )


def _validated_hint(raw: str) -> str:
    """Reject an unknown hint loudly rather than writing a row the
    agent's category filters would silently never match."""
    try:
        return RemediationHint(raw).value
    except ValueError:
        valid = ", ".join(sorted(h.value for h in RemediationHint))
        raise ValueError(
            f"unknown remediation_hint {raw!r}; expected one of: {valid}"
        ) from None


async def _fixture_owner(ctx: ToolContext, tenant_id: uuid.UUID) -> User:
    """Any real user in the caller's tenant satisfies the Job FK.

    Deliberately reuses `create_bad_data_job`'s chaos-owner fallback
    rather than duplicating it, so both hooks lazy-create the same
    recognisable user on an unseeded tenant and the existing
    chaos-owner cleanup reaches both.
    """
    from app.mcp.tools.chaos.create_bad_data_job import _ensure_chaos_owner

    user = (
        await ctx.db.execute(
            select(User).where(User.tenant_id == tenant_id).limit(1)
        )
    ).scalar_one_or_none()
    if user is None:
        user = await _ensure_chaos_owner(ctx, tenant_id)
    return user
