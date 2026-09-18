"""`seed_dlq_messages` — create N DLQ rows with declared types and hints.

Platform half of commander ADR 0010: a scenario declares the DLQ content it is
graded against instead of pivoting onto a standing pool. Writing `dead_letter`
rows into a live database is fault injection, so it lives under chaos and
inherits ADR 0008's triple gate. Rows carry `payload.seeded_fixture = true`, so
the reset sweep DELETEs them rather than leaving `cancelled` litter.
"""

import uuid
from typing import Literal

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.lab.dlq_failure_stories import default_error_for
from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext
from app.models.enums import JobStatus, JobType, RemediationHint
from app.models.job import Job
from app.models.user import User
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

logger = get_logger(__name__)

# Marker the reset sweep DELETEs on, imported from here by every hook that
# writes a declared fixture so there is one spelling. `chaos_fixture` is a
# different key: provenance ("which hook wrote this"), not disposal.
SEEDED_FIXTURE_MARKER = "seeded_fixture"

# Error strings come from `app.lab.dlq_failure_stories`, one table shared with
# the sibling hooks and the seed script, whose pairings
# `tests/unit/test_dlq_text_coherence.py` checks. They used to be a dict here
# that paired `replay_safe` with a permanent fault (WO-R2-146, efdc3b2a9864).


class SeedDlqHintError(AppError):
    """An unknown `remediation_hint` reached the handler body.

    Unreachable behind the input `Literal`; it exists so a direct caller
    gets a typed refusal, not R2-16's `-32603`."""

    status_code = 400
    error_code = "unknown_remediation_hint"


# Spelled out, not derived from `RemediationHint`: these strings are baked into
# the inputSchema. `test_seed_dlq_hint_literal_matches_the_enum` guards drift.
_HINT_VALUES = Literal["replay_safe", "wait_and_replay", "human_required"]


class SeedDlqMessagesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remediation_hint: _HINT_VALUES = Field(
        description=(
            "Category stamped on every row: `replay_safe`, "
            "`wait_and_replay`, or `human_required`. Drives which "
            "branch of the agent's remediation logic the rows exercise. "
            "An unrecognised value is rejected at parse time as an "
            "invalid-params error, not a tool crash."
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
    error_message = inp.error_message or default_error_for(hint)

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
    """Reject an unknown hint rather than write a row no filter matches.
    `SeedDlqHintError` says why the type matters."""
    try:
        return RemediationHint(raw).value
    except ValueError:
        valid = ", ".join(sorted(h.value for h in RemediationHint))
        raise SeedDlqHintError(
            f"unknown remediation_hint {raw!r}; expected one of: {valid}"
        ) from None


async def _fixture_owner(ctx: ToolContext, tenant_id: uuid.UUID) -> User:
    """Any real user in the caller's tenant satisfies the Job FK; reuses
    `create_bad_data_job`'s chaos-owner fallback so one cleanup covers both."""
    from app.mcp.tools.chaos.create_bad_data_job import _ensure_chaos_owner

    user = (
        await ctx.db.execute(
            select(User).where(User.tenant_id == tenant_id).limit(1)
        )
    ).scalar_one_or_none()
    if user is None:
        user = await _ensure_chaos_owner(ctx, tenant_id)
    return user
