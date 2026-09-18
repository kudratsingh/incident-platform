"""`create_bad_data_job` — inject a bad-data DLQ row, already classified
`human_required` or not classified at all.

Bad-data half of the lab's permanent-fault pair: a CSV parse failure here, a
schema violation in `poison_message`, neither replay-safe (WO-R2-166). Text
comes from `app.lab.dlq_failure_stories` so hint and text agree (WO-R2-146);
the row that deliberately disagrees is `create_mislabeled_dlq_job`'s. Seed it
`unclassified` when the drill grades the fence itself, or `mark_dlq_permanent`
only re-sets the value the row already carries (WO-R2-158).

Ids are `uuid5(namespace, f"{tenant_id}:{fixture_name}")` — pinnable before the
run (commander cmd #187), per-tenant so the RLS-scoped probe below cannot miss
a sibling's row and collide on the primary key (409, not 500). A repeat is
idempotent while the row matches and refused once it has drifted. Rows carry
`payload.seeded_fixture = true`, so `_delete_seeded_dlq_fixtures` DELETEs them
instead of leaving a `cancelled` copy per run (ADR 0012 rule 2);
`chaos_fixture` stays for provenance. ADR 0008 gated.
"""


import uuid
from typing import Final, Literal

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.lab.dlq_failure_stories import story
from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext
from app.mcp.tools.chaos.seed_dlq_messages import (
    SEEDED_FIXTURE_MARKER,
    _validated_hint,
)
from app.models.enums import JobStatus, JobType, RemediationHint, UserRole
from app.models.job import Job
from app.models.user import User
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

logger = get_logger(__name__)

# uuid5 namespace for bad-data fixture ids: uuid5(ns,
# f"{tenant_id}:{fixture_name}"). Distinct from the eval seed's (aaaaaaaa-…)
# and every sibling hook's, so one `fixture_name` cannot collide across them.
_NAMESPACE = uuid.UUID("dddddddd-bad0-4000-8000-000000000000")

# Sentinel meaning "write NULL into remediation_hint", a word rather than only
# JSON `null` because it is baked into the inputSchema (and omitting the field
# keeps the `human_required` default). `Final` so mypy infers the `Literal`.
UNCLASSIFIED: Final = "unclassified"

# Both stories are bad-data texts; the only difference is whether
# anybody has classified the row.
_STORY_KEY_FOR_HINT: dict[str | None, str] = {
    RemediationHint.HUMAN_REQUIRED.value: "csv_bad_row",
    None: "unclassified_csv_bad_row",
}


class CreateBadDataJobError(AppError):
    status_code = 409
    error_code = "bad_data_fixture_name_in_use"


class CreateBadDataJobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture_name: str = Field(
        default="bad-data-job",
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description=(
            "Name the row's id derives from — "
            "uuid5(dddddddd-bad0-4000-8000-000000000000, "
            "'{tenant_id}:{fixture_name}') — so a caller can pin the id "
            "before invoking. Ids are scoped to the calling tenant, so "
            "the same name in two tenants creates two independent rows. "
            "Re-invoking with the same name is idempotent while the row "
            "still matches what the call declares; refused with "
            "`bad_data_fixture_name_in_use` once it has drifted (someone "
            "fenced it, a replay moved it out of `dead_letter`, or this "
            "call declares a different `remediation_hint`)."
        ),
    )
    job_type: str = Field(
        default=JobType.CSV_UPLOAD.value,
        description=(
            "Type stamped on the synthetic job. Should match a real "
            "processor type so the agent's downstream reasoning stays "
            "plausible."
        ),
    )
    # A subset of `RemediationHint`: this hook writes one permanent bad-data
    # fault, and `replay_safe` on that text is WO-R2-146; `seed_dlq_messages`
    # offers all three hints with texts that fit.
    remediation_hint: Literal["human_required", "unclassified"] | None = Field(
        default=RemediationHint.HUMAN_REQUIRED.value,
        description=(
            "Whether the row arrives already classified. "
            "`human_required` (the default) stamps the category, so "
            "`replay_dlq_by_category` refuses the row on sight and there "
            "is nothing left for a reader to decide. `unclassified` — or "
            "JSON `null`, which means the same thing — writes "
            "`remediation_hint = NULL`: the row carries a bad-data error "
            "text that nothing has categorised, which is what an "
            "organically dead-lettered job looks like here (LLM triage is "
            "off by default). Use `unclassified` when the point is that "
            "the reader has to classify and fence the row itself; a row "
            "seeded `human_required` cannot measure that, because the "
            "fence would be setting the value it already has. Omitting "
            "the field is not the same as passing null: omission keeps "
            "the `human_required` default."
        ),
    )
    error_message: str | None = Field(
        default=None,
        max_length=2048,
        description=(
            "Realistic error string. Defaults to the bad-data text that "
            "matches the declared `remediation_hint` — a permanent data "
            "fault, which is what makes a `human_required` hint honest and "
            "what gives an `unclassified` row something a reader can "
            "actually classify. Overriding it with a transient-sounding "
            "error contradicts a `human_required` hint and invites a "
            "replay that cannot work; under `unclassified` it invites a "
            "replay nothing has authorised."
        ),
    )


class CreateBadDataJobOutput(BaseModel):
    job_id: str = Field(
        description="The dead-lettered job — the id to read with "
        "`list_dlq_messages` and to fence with `mark_dlq_permanent`. "
        "Derived from the tenant and `fixture_name`, so a caller can "
        "compute it in advance."
    )
    fixture_name: str
    remediation_hint: str | None = Field(
        description="The category actually stamped on the row: "
        "`human_required`, or null when the call declared "
        "`unclassified`."
    )
    created: bool = Field(
        description="False when the call was an idempotent repeat that "
        "found the row already present and still matching."
    )
    accepted: bool


@chaos_tool(
    "create_bad_data_job",
    description=(
        "Inject a synthetic dead-letter row whose error text is a "
        "permanent data fault (a non-numeric value in an integer CSV "
        "column). Complement to `poison_message`, whose row carries a "
        "schema violation instead; neither is replay-safe, and neither "
        "can be asked to be. `remediation_hint` decides whether the "
        "row arrives classified: `human_required` (default) stamps the "
        "category, so `replay_dlq_by_category` refuses it immediately; "
        "`unclassified` (or null) leaves `remediation_hint` NULL, so the "
        "row has to be read and classified before anything can act on it "
        "— which is the only shape that makes a fence by "
        "`mark_dlq_permanent` an observable action rather than a value "
        "the row already had. The id derives deterministically from the "
        "calling tenant and `fixture_name`, so a caller can pin it before "
        "invoking and the same name in another tenant is a separate row; "
        "a repeat call is idempotent while the row still matches, and "
        "refused once it has drifted. The row is tagged as a seeded "
        "fixture and DELETEd by the next environment reset. Written "
        "directly to `jobs`; doesn't touch Kafka."
    ),
    input_model=CreateBadDataJobInput,
    output_model=CreateBadDataJobOutput,
    blast_radius=BlastRadius.ENVIRONMENT_WIDE,
)
async def create_bad_data_job(
    inp: CreateBadDataJobInput, ctx: ToolContext
) -> CreateBadDataJobOutput:
    tenant_id = ctx.principal.tenant_id
    hint = _declared_hint(inp.remediation_hint)
    job_id = fixture_id(tenant_id, inp.fixture_name)

    # RLS-scoped primary-key read: a repeat returns the same row, a
    # drifted one is refused.
    existing = (
        await ctx.db.execute(select(Job).where(Job.id == job_id))
    ).scalar_one_or_none()
    if existing is not None:
        _assert_matches(inp.fixture_name, existing, hint, tenant_id)
        return CreateBadDataJobOutput(
            job_id=str(job_id),
            fixture_name=inp.fixture_name,
            remediation_hint=hint,
            created=False,
            accepted=True,
        )

    # Any real user in the caller's tenant satisfies the Job FK; an unseeded
    # tenant gets a chaos owner in the SAME tenant, never DEFAULT_TENANT_ID,
    # which would split jobs.tenant_id from users.tenant_id (ADR 0003).
    user = (
        await ctx.db.execute(
            select(User).where(User.tenant_id == tenant_id).limit(1)
        )
    ).scalar_one_or_none()
    if user is None:
        user = await _ensure_chaos_owner(ctx, tenant_id)

    job = Job(
        id=job_id,
        tenant_id=tenant_id,
        user_id=user.id,
        type=inp.job_type,
        status=JobStatus.DEAD_LETTER.value,
        # `seeded_fixture` is the disposal marker the reset sweep DELETEs
        # on; `chaos_fixture` stays for provenance (module docstring).
        payload={
            SEEDED_FIXTURE_MARKER: True,
            "chaos_fixture": "bad_data_job",
            "fixture_name": inp.fixture_name,
        },
        retry_count=3,
        error_message=inp.error_message or _default_error_for(hint),
        remediation_hint=hint,
    )
    ctx.db.add(job)
    await ctx.db.flush()

    logger.warning(
        "chaos create_bad_data_job injected",
        extra={
            "job_id": str(job.id),
            "tenant_id": str(tenant_id),
            "fixture_name": inp.fixture_name,
            "job_type": inp.job_type,
            "remediation_hint": hint,
        },
    )
    return CreateBadDataJobOutput(
        job_id=str(job.id),
        fixture_name=inp.fixture_name,
        remediation_hint=hint,
        created=True,
        accepted=True,
    )


def fixture_id(tenant_id: uuid.UUID, fixture_name: str) -> uuid.UUID:
    """The row's deterministic id, exported so a test or a scenario derives
    it instead of transcribing the recipe. Per-tenant (module docstring)."""
    return uuid.uuid5(_NAMESPACE, f"{tenant_id}:{fixture_name}")


def _declared_hint(raw: str | None) -> str | None:
    """The value to write into `remediation_hint`.

    `None` and the `"unclassified"` sentinel both mean NULL. Anything else goes
    through `seed_dlq_messages._validated_hint`, whose typed `SeedDlqHintError`
    keeps a direct Python caller's bad input off the `-32603` path (R2-16).
    """
    if raw is None or raw == UNCLASSIFIED:
        return None
    return _validated_hint(raw)


def _default_error_for(hint: str | None) -> str:
    """The canonical bad-data text for a declared hint.

    Pinned by story key: `default_error_for(None)` says nothing about class.
    """
    return story(_STORY_KEY_FOR_HINT[hint]).error_message


def _assert_matches(
    fixture_name: str,
    existing: Job,
    hint: str | None,
    tenant_id: uuid.UUID,
) -> None:
    """Idempotent repeat vs. drifted row.

    Still `dead_letter`, in the caller's tenant, with exactly the declared hint
    is a no-op. Anything else is refused: a moved `remediation_hint` means
    somebody fenced it, which is the action an `unclassified` drill measures,
    and returning `created=False` would hand the next run a pre-fenced world
    and grade it clean.
    """
    drift: list[str] = []
    if existing.tenant_id != tenant_id:
        # Unreachable while ids are tenant-derived; only a non-RLS
        # session could see a foreign row here.
        drift.append("owned by another tenant")
    if existing.status != JobStatus.DEAD_LETTER.value:
        drift.append(
            f"status is {existing.status!r}, not "
            f"{JobStatus.DEAD_LETTER.value!r}"
        )
    if existing.remediation_hint != hint:
        drift.append(
            f"remediation_hint is {existing.remediation_hint!r}, not the "
            f"declared {hint!r}"
        )
    if drift:
        raise CreateBadDataJobError(
            f"fixture_name {fixture_name!r} is already in use by job "
            f"{existing.id} and no longer matches the declared fixture "
            f"({'; '.join(drift)}). Pick a different fixture_name or "
            "reset the environment; this hook never rewrites existing "
            "rows."
        )


# Deterministic email + an unusable password (bcrypt refuses "!") so logins
# fail cleanly and cleanup can grep by prefix; `is_active` False hides them.
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
