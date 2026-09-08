"""
`create_bad_data_job` — inject a realistic bad-data DLQ entry, either
already classified `human_required` or not classified at all.

The persistent-bug counterpart to `poison_message` (which produces
`replay_safe` entries). Doesn't touch Kafka — writes directly to `jobs`
with `status=dead_letter` and an error string from
`app.lab.dlq_failure_stories`, so the row's text and its hint say the
same thing (WO-R2-146).

## Why `remediation_hint` is an argument now

Until v0.6.1 this hook always stamped `remediation_hint=human_required`.
That is the right shape for a scenario that wants the row *already*
fenced — `replay_dlq_by_category` refuses it, and the agent's
escalate-not-replay branch is reachable immediately.

It is the wrong shape for an escalation drill that grades the fence
itself. `dlq_human_required_escalates` asks the agent to read a failure,
decide it is not replayable, fence it with `mark_dlq_permanent`, and
escalate. Seeded pre-classified, the middle step is a no-op the eval
cannot see: the hint is already the value the fence would set, so before
WO-R2-158 `mark_dlq_permanent` took its `already_marked` branch and wrote
nothing at all — not the row, not even an audit row. An agent that
fenced and an agent that skipped the fence left an identical world.

So the hook takes the hint as an argument:

  * `human_required` (the default, and the pre-v0.6.2 behaviour) — the
    row arrives classified. Nothing left to decide.
  * `unclassified` (or JSON `null`) — the row arrives with
    `remediation_hint = NULL` and a bad-data error text. Nothing has
    classified it, so the agent has to read the error, conclude a replay
    cannot fix a bad row in the stored payload, and raise the fence
    itself. That fence is now a real write with a real audit row, so the
    drill measures an action rather than a coincidence.

A null hint with a permanent-fault error text is a coherent pair, not a
loosened screen: a hint is a classification and an error text is a
symptom, and "nobody has classified this" does not disagree with "the
symptom is a bad row". It is also the normal state of an organically
dead-lettered job here, because LLM triage is off by default. See the
`None` bullet in `app.lab.dlq_failure_stories`.

## Deterministic ids

The row's id is `uuid5(namespace, f"{tenant_id}:{fixture_name}")` — the
same convention as `create_stuck_dag` and `scripts/seed_eval_fixtures.py`,
with its own namespace — so a scenario can pin the id in YAML before the
hook ever runs, given the tenant it will run as. It has to: the drill
grades *which* row the agent fenced, and a random id cannot be named in a
claim written before the run (commander cmd #187).

The tenant is in the key for the same reason it is in `create_stuck_dag`'s:
the idempotency probe below runs on the RLS-scoped MCP session, so a row
another tenant created under the same `fixture_name` would be invisible to
it and the INSERT would collide on the primary key — a 500 where the
contract promises a 409. Per-tenant ids make that collision
unrepresentable.

Re-invoking with the same `fixture_name` is idempotent while the row still
matches what this call declares. Once it has drifted — the agent fenced
it, a replay moved it out of `dead_letter`, or the call now declares a
different hint — the hook refuses rather than rewriting history. Pick a
fresh `fixture_name` or reset the environment.

## Disposal

The row is tagged `payload.seeded_fixture = true`, so the reset sweep
(`scripts/reset_eval_state.py::_delete_seeded_dlq_fixtures`) DELETEs it.
That is a change of disposal class: these rows used to be *cancelled* by
`_sweep_nonfixture_dlq` on the grounds that a randomly-idded chaos row
attached to a real user reads as that user's history. A row with a
scenario-pinned id, declared by name, is scaffolding the same way
`create_stuck_dag`'s chain is — and leaving a `cancelled` copy behind per
run is litter, not history (ADR 0012 rule 2). `chaos_fixture` stays in the
payload beside the marker so the row's provenance is still readable.

Chaos-only surface: gated behind `CHAOS_ENABLED=true` + `chaos:invoke`
scope + `environment_wide` blast radius label. See ADR 0008 gating.
"""


import uuid
from typing import Literal

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

# uuid5 namespace for bad-data fixture ids. Fixed and documented so a
# scenario can precompute the id it pins:
# uuid5(ns, f"{tenant_id}:{fixture_name}").
# Distinct from the eval seed's namespace (aaaaaaaa-…) and
# `create_stuck_dag`'s (cccccccc-…) so these ids can never collide with a
# boot-seeded fixture or a chain node.
_NAMESPACE = uuid.UUID("dddddddd-bad0-4000-8000-000000000000")

# The sentinel that means "write NULL into remediation_hint". Spelled as a
# word rather than only accepting JSON `null` because the value is baked
# verbatim into the tool's inputSchema, and an enum of two words is
# unambiguous where a nullable string leaves a caller guessing whether
# omitting the field and passing null mean the same thing (they do not —
# omitting it keeps the pre-v0.6.2 `human_required` behaviour).
UNCLASSIFIED = "unclassified"

# The story each declared hint stamps. Both are bad-data texts on purpose:
# the drill's whole subject is a row a reader can classify, and the only
# difference between the two rows is whether anybody already has.
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
    # Deliberately a *subset* of `RemediationHint` plus the sentinel, not
    # the whole enum: this hook writes one kind of failure — a permanent
    # bad-data fault — and `replay_safe` or `wait_and_replay` on that text
    # is exactly the self-contradiction WO-R2-146 was filed for. A scenario
    # wanting those hints calls `seed_dlq_messages`, which offers all three
    # and pairs each with a text that fits.
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
        "column). Complement to `poison_message`, which produces "
        "`replay_safe` entries. `remediation_hint` decides whether the "
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

    # Primary-key read, RLS-scoped like every other tool call. An
    # idempotent repeat returns the same row; a drifted one is refused
    # rather than rewritten — see `_assert_matches`.
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

    # Prefer any real user in the caller's tenant to satisfy the Job
    # FK. When the tenant is unseeded (common in local dev / fresh
    # eval env), lazy-create a chaos-owned user in the SAME tenant.
    # The pre-v0.4.6 shape fell back to a user from DEFAULT_TENANT_ID,
    # violating the tenant-isolation invariant (jobs.tenant_id and
    # users.tenant_id ended up pointing at different tenants). See
    # ADR 0003 (RLS as defense-in-depth). `seed_dlq_messages._fixture_owner`
    # is the same rule, and reuses `_ensure_chaos_owner` below so the two
    # declared-fixture hooks lazy-create the same recognisable user.
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
    """The row's deterministic id.

    Exported so a test — or a scenario's own precompute — derives it the
    same way the hook does instead of transcribing the recipe.
    Per-tenant: see the module docstring for why widening the RLS-scoped
    probe would be the wrong repair for the collision this prevents.
    """
    return uuid.uuid5(_NAMESPACE, f"{tenant_id}:{fixture_name}")


def _declared_hint(raw: str | None) -> str | None:
    """The value to write into `remediation_hint`.

    `None` and the `"unclassified"` sentinel both mean "write NULL"; the
    two spellings exist because a scenario file that means an empty hint
    naturally writes `null`, while the inputSchema an agent reads is
    clearer as a two-word enum.

    Anything else goes through `seed_dlq_messages._validated_hint`, which
    raises the typed `SeedDlqHintError` (400, `unknown_remediation_hint`).
    The `Literal` on the input model should make that unreachable over MCP
    — it exists for a *direct* Python caller (a script, a test), because a
    bare `ValueError` here renders as `-32603 internal tool error` with an
    `mcp tool crashed` log line for what is really invalid input (R2-16).
    """
    if raw is None or raw == UNCLASSIFIED:
        return None
    return _validated_hint(raw)


def _default_error_for(hint: str | None) -> str:
    """The canonical bad-data text for a declared hint.

    Pins a story by *key* rather than taking each hint's canonical
    default, because `default_error_for(None)` is deliberately the
    says-nothing-about-its-class text and this hook wants the opposite: a
    symptom a reader can act on, under a hint that has not acted on it.
    """
    return story(_STORY_KEY_FOR_HINT[hint]).error_message


def _assert_matches(
    fixture_name: str,
    existing: Job,
    hint: str | None,
    tenant_id: uuid.UUID,
) -> None:
    """Idempotent repeat vs. drifted row.

    A repeat that finds the row still `dead_letter`, in the caller's
    tenant, carrying exactly the hint this call declares is a no-op.
    Anything else is refused, because re-manufacturing would mean
    rewriting a row that is now evidence:

      * `remediation_hint` moved — somebody fenced it, which is the very
        action a drill seeded `unclassified` exists to measure. Silently
        returning `created=False` here would hand the next run a
        pre-fenced world and grade it clean.
      * `status` moved — a replay took the row out of `dead_letter`.
      * this call declares a different hint than the stored row carries,
        so returning the row would report a fixture the caller did not
        ask for.

    Same rule and same wording as `create_stuck_dag._assert_intact`; that
    one keys on status alone because a chain's drift shows up there, while
    this row's load-bearing drift is the hint.
    """
    drift: list[str] = []
    if existing.tenant_id != tenant_id:
        # Unreachable while ids are tenant-derived; kept because it is the
        # invariant the derivation exists to guarantee, and a non-RLS
        # session (a script, a superuser) is the one caller that could
        # still see a foreign row here.
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
