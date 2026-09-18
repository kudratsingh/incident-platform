"""`create_stuck_dag` — manufacture a dependency chain that is genuinely stuck.

Three shapes (WO-R3-274, ADR 0029). Default `root_status="dead_letter"`:
upstream completed → root dead-lettered → N descendants `waiting`, stuck by the
platform's own rules (a child promotes only at `unmet_count == 0`, `dead_letter`
is terminal, and no `saga_id` means nothing cancels it). `root_status="completed"`
strands the chain with no dead-letter row to replay, and does NOT hold alone: it
needs `kill_consumer('dependency-resolver')` AND
`pause_control_loop('resume_unblocked_waiting')`, and only the pause's TTL heals
it (WO-R3-213, ADR 0027's 2026-09-17 amendment). Adding `failed_step=K`
dead-letters descendant K under a live root, and that shape holds alone.

Rows are inserted, not transitioned, so #165's CANCELLED cascade (ADR 0022 §3)
never fires: read the result as a declared instance of the pre-#165 stuck mode,
not live behaviour. `replay_dlq_by_ids` on the root unsticks it, `pause_dag` only
stabilizes (commander ADR 0026). The dead-lettered row's text derives from the
declared `remediation_hint` through `app.lab.dlq_failure_stories` (WO-R2-146);
`child_age_seconds` backdates the whole chain, parents included.

Ids are `uuid5(ns, f"{tenant_id}:{chain_name}:{role}")` — pinnable, per-tenant,
and independent of the shape, so re-invoking a `chain_name` in another shape is
drift (`stuck_chain_name_in_use`). Rows carry `payload.seeded_fixture = true`, so
the reset DELETEs the chain and edges CASCADE (ADR 0012 rule 2). ADR 0008 gated;
writes `jobs` / `job_dependencies`, never Kafka.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal, Self

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.lab.dlq_failure_stories import default_error_for
from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext

# Reuses seed_dlq_messages' marker, owner fallback and hint validation so the
# declared-fixture hooks stay in lockstep; error strings now come from
# `app.lab.dlq_failure_stories`.
from app.mcp.tools.chaos.seed_dlq_messages import (
    SEEDED_FIXTURE_MARKER,
    _fixture_owner,
    _validated_hint,
)
from app.models.enums import JobStatus, JobType
from app.models.job import Job
from app.repositories.job_dependency import JobDependencyRepository
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError
from sqlalchemy import select

logger = get_logger(__name__)

# uuid5 namespace for chain ids: root = uuid5(ns,
# f"{tenant_id}:{chain_name}:root"). Distinct from the eval seed's.
_NAMESPACE = uuid.UUID("cccccccc-57ac-4000-8000-000000000000")

# Upper bound on `waiting_steps`, and so on the chain's whole id space: the
# integrity probe enumerates every id this hook could ever have written for a
# chain_name, to notice descendants left by a longer earlier call.
_MAX_WAITING_STEPS = 10

# Upper bound on `child_age_seconds` — one day. An unbounded backdate would put
# a fixture outside `search_traces(since_hours=...)`'s widest window (168 h) and
# outside any age an operator would believe.
_MAX_CHILD_AGE_SECONDS = 86_400


def _chain_id(tenant_id: uuid.UUID, chain_name: str, role: str) -> uuid.UUID:
    """Per-tenant so two tenants drilling the same `chain_name` can
    never land on the same primary key — see the module docstring for
    why this is preferred over widening the RLS-scoped probe."""
    return uuid.uuid5(_NAMESPACE, f"{tenant_id}:{chain_name}:{role}")


class CreateStuckDagError(AppError):
    status_code = 409
    error_code = "stuck_chain_name_in_use"


class CreateStuckDagInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chain_name: str = Field(
        default="stuck-dag",
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description=(
            "Name every id in the chain derives from — "
            "uuid5(cccccccc-57ac-4000-8000-000000000000, "
            "'{tenant_id}:{chain_name}:root') is the root id, "
            "':upstream' the completed parent, ':step-1'..':step-N' the "
            "waiting descendants — so callers can pin ids before "
            "invoking. Ids are scoped to the calling tenant, so the "
            "same name in two tenants builds two independent chains. "
            "Re-invoking with the same name is idempotent while the "
            "chain is intact; refused once any row has drifted, or if "
            "the stored chain has more steps than this call asks for."
        ),
    )
    waiting_steps: int = Field(
        default=2,
        ge=1,
        le=_MAX_WAITING_STEPS,
        description="How many descendants are chained in `waiting` "
        "behind the dead-lettered root. Default 2 mirrors the "
        "three-visible-node shape `get_dag_state` returns.",
    )
    job_type: str = Field(
        default=JobType.BULK_API_SYNC.value,
        description="Type stamped on every row. Should match a real "
        "processor type so replaying the root actually executes.",
    )
    remediation_hint: str = Field(
        default="wait_and_replay",
        description="Category stamped on the dead-lettered root: "
        "`replay_safe`, `wait_and_replay`, or `human_required`. "
        "`human_required` makes the chain unrecoverable through the "
        "replay guardrails — reserve it for escalation drills.",
    )
    error_message: str | None = Field(
        default=None,
        max_length=2048,
        description="Error string on the chain's dead-lettered row. "
        "Defaults to a realistic string matching the declared "
        "remediation_hint. Unused when the chain has no dead-lettered "
        "row (`root_status='completed'` with no `failed_step`).",
    )
    root_status: Literal["dead_letter", "completed"] = Field(
        default="dead_letter",
        description=(
            "What the root is. `dead_letter` (default) is the original "
            "chain: retries exhausted, descendants held behind a terminal "
            "row, stuck by the platform's own rules. `completed` strands "
            "the chain instead — root `completed`, descendants `waiting`, "
            "and NO dead-letter row anywhere in it, so there is nothing "
            "to replay. That shape does NOT hold by itself: with the root "
            "completed, step-1 has no unmet parent, so the "
            "`dependency-resolver` consumer group and the resume sweep "
            "each promote it within seconds. Pair it with "
            "`kill_consumer('dependency-resolver')` AND "
            "`pause_control_loop('resume_unblocked_waiting')`."
        ),
    )
    child_age_seconds: int = Field(
        default=0,
        ge=0,
        le=_MAX_CHILD_AGE_SECONDS,
        description=(
            "Backdate `created_at` (and `updated_at`) on every row in the "
            "chain by this many seconds, so 'this child has been waiting a "
            "long time' is true on the first read rather than after a wait. "
            "0 (default) writes the rows at the current time, as before. "
            "The whole chain moves together, not only the descendants: a "
            "child that is older than its own parent is a graph the "
            "platform cannot produce. Dispatch times (`started_at`, "
            "`completed_at`) stay NULL on every row either way. Max one day."
        ),
    )
    failed_step: int | None = Field(
        default=None,
        ge=1,
        le=_MAX_WAITING_STEPS,
        description=(
            "Dead-letter descendant N instead of the root, leaving exactly "
            "one dead-letter row in the chain, below a root that succeeded. "
            "Only valid with `root_status=\"completed\"`, and must be within "
            "`waiting_steps`; anything else is an invalid argument. The "
            "descendants before it are `completed` — a job cannot have been "
            "dispatched before its own parent finished — and the "
            "descendants after it stay `waiting` behind a terminal row. "
            "Unlike the bare `completed` chain, this one holds on its own."
        ),
    )

    @model_validator(mode="after")
    def _failed_step_needs_a_completed_root(self) -> Self:
        """Two inputs that are not independent, refused before any write.

        A dead-lettered root *and* a dead-lettered descendant is two faults in
        one chain, and a `failed_step` past the end would address a row this
        call is not creating. Both are JSON-RPC invalid params, not
        `stuck_chain_name_in_use` — nothing about the environment is wrong.
        `PydanticCustomError` for the reason
        `saturate_redis._bound_total_footprint` gives.
        """
        if self.failed_step is None:
            return self
        if self.root_status != "completed":
            raise PydanticCustomError(
                "failed_step_needs_a_completed_root",
                "failed_step needs root_status='completed' (got "
                "'{root_status}'): a chain cannot have both a dead-lettered "
                "root and a dead-lettered descendant.",
                {"root_status": self.root_status},
            )
        if self.failed_step > self.waiting_steps:
            raise PydanticCustomError(
                "failed_step_past_the_end_of_the_chain",
                "failed_step={failed_step} is past the end of a chain with "
                "waiting_steps={waiting_steps}.",
                {
                    "failed_step": self.failed_step,
                    "waiting_steps": self.waiting_steps,
                },
            )
        return self


class CreateStuckDagOutput(BaseModel):
    root_job_id: str = Field(
        description="The job holding the chain — the id to alert on, "
        "probe with `get_dag_state`, pause, or (when it is the "
        "dead-lettered one) replay."
    )
    completed_parent_id: str
    waiting_job_ids: list[str] = Field(
        description="The descendants this call left in status `waiting`, "
        "in chain order. Equal to `step_job_ids` unless `failed_step` "
        "took some of them out of `waiting`."
    )
    step_job_ids: list[str] = Field(
        description="Every descendant id, in chain order, whatever status "
        "it carries — `waiting_job_ids` plus the ones `failed_step` made "
        "`completed` or `dead_letter`."
    )
    dead_letter_job_id: str | None = Field(
        default=None,
        description="The chain's one dead-lettered row: the root by "
        "default, descendant `failed_step` when asked for, and null when "
        "the chain deliberately has none.",
    )
    chain_name: str
    created: bool = Field(
        description="False when the call was an idempotent repeat that "
        "found the chain already manufactured and intact."
    )
    accepted: bool


@chaos_tool(
    "create_stuck_dag",
    description=(
        "Manufacture a stuck dependency chain, in one of three shapes. "
        "DEFAULT (`root_status='dead_letter'`): a completed upstream "
        "parent, a dead-lettered root (retries exhausted), and N "
        "descendants held in `waiting` behind it. Stuck by the "
        "platform's own rules — the resolver promotes a child only when "
        "every parent is `completed`, and `dead_letter` is terminal — "
        "so the chain will not drain on its own. Compensators: "
        "`pause_dag` stabilizes (TTL self-cleans) and `replay_dlq_by_ids` "
        "on `root_job_id` genuinely unsticks it (the replayed root "
        "completes and the resolver promotes the descendants).\n"
        "STRANDED (`root_status='completed'`): root `completed`, "
        "descendants `waiting`, and no dead-letter row anywhere in the "
        "chain — so nothing in it can be replayed. This shape does NOT "
        "hold by itself: the root being `completed` leaves step-1 with no "
        "unmet parent, so the `dependency-resolver` consumer group "
        "promotes it on the next `job.completed` and the resume sweep "
        "promotes it within about ten seconds regardless. It needs BOTH "
        "`kill_consumer('dependency-resolver')` and "
        "`pause_control_loop('resume_unblocked_waiting')` to stay "
        "stranded, and only the second one's TTL heals it.\n"
        "DOWNSTREAM FAILURE (`root_status='completed'` + `failed_step=N`): "
        "the same, except descendant N is dead-lettered and the "
        "descendants before it are `completed` — exactly one dead-letter "
        "row, under a root that succeeded. This shape holds on its own.\n"
        "`child_age_seconds` backdates the whole chain so it reads as "
        "long-waiting on the first probe. Observe any of them with "
        "`get_dag_state(root_job_id)`, `search_traces(status='waiting')` "
        "and `list_dlq_messages`. Ids derive deterministically from the "
        "calling tenant and `chain_name` — and NOT from the shape, so "
        "re-invoking an existing `chain_name` with a different "
        "`root_status` is refused as drift rather than rewriting it. The "
        "same name in another tenant is a separate chain; rows are tagged "
        "as seeded fixtures and deleted by the next environment reset."
    ),
    input_model=CreateStuckDagInput,
    output_model=CreateStuckDagOutput,
    blast_radius=BlastRadius.ENVIRONMENT_WIDE,
)
async def create_stuck_dag(
    inp: CreateStuckDagInput, ctx: ToolContext
) -> CreateStuckDagOutput:
    hint = _validated_hint(inp.remediation_hint)
    tenant_id = ctx.principal.tenant_id

    upstream_id = _chain_id(tenant_id, inp.chain_name, "upstream")
    root_id = _chain_id(tenant_id, inp.chain_name, "root")
    all_step_ids = [
        _chain_id(tenant_id, inp.chain_name, f"step-{i}")
        for i in range(1, _MAX_WAITING_STEPS + 1)
    ]
    step_ids = all_step_ids[: inp.waiting_steps]

    # The shape, in one place: which status each row carries, and which single
    # row (if any) is the dead-lettered one the hint and error text belong to.
    root_state = (
        JobStatus.DEAD_LETTER.value
        if inp.root_status == "dead_letter"
        else JobStatus.COMPLETED.value
    )
    expected: dict[uuid.UUID, str] = {
        upstream_id: JobStatus.COMPLETED.value,
        root_id: root_state,
    }
    for position, step_id in enumerate(step_ids, start=1):
        expected[step_id] = _step_status(position, inp.failed_step)

    dead_letter_id: uuid.UUID | None = None
    if inp.root_status == "dead_letter":
        dead_letter_id = root_id
    elif inp.failed_step is not None:
        dead_letter_id = step_ids[inp.failed_step - 1]

    waiting_ids = [
        sid for sid in step_ids if expected[sid] == JobStatus.WAITING.value
    ]

    # Probe the whole id space, not just this call's rows, so a longer chain
    # from an earlier call shows up as extra descendants (R2-55).
    existing = (
        (
            await ctx.db.execute(
                select(Job).where(
                    Job.id.in_([upstream_id, root_id, *all_step_ids])
                )
            )
        )
        .scalars()
        .all()
    )
    if existing:
        _assert_intact(inp.chain_name, expected, existing, tenant_id)
        return CreateStuckDagOutput(
            root_job_id=str(root_id),
            completed_parent_id=str(upstream_id),
            waiting_job_ids=[str(sid) for sid in waiting_ids],
            step_job_ids=[str(sid) for sid in step_ids],
            dead_letter_job_id=(
                str(dead_letter_id) if dead_letter_id is not None else None
            ),
            chain_name=inp.chain_name,
            created=False,
            accepted=True,
        )

    user = await _fixture_owner(ctx, tenant_id)
    # Derived from the declared hint: a `replay_safe` chain whose root said
    # SchemaValidationError made the agent escalate, correctly (WO-R2-146).
    error_message = inp.error_message or default_error_for(hint)

    # Passed only for a backdate; otherwise the server default stamps
    # them.
    timestamps: dict[str, datetime] = {}
    if inp.child_age_seconds:
        backdated = datetime.now(UTC) - timedelta(seconds=inp.child_age_seconds)
        timestamps = {"created_at": backdated, "updated_at": backdated}

    for job_id, status in expected.items():
        is_dead_letter = job_id == dead_letter_id
        ctx.db.add(
            Job(
                id=job_id,
                tenant_id=tenant_id,
                user_id=user.id,
                type=inp.job_type,
                status=status,
                payload={
                    SEEDED_FIXTURE_MARKER: True,
                    "chain": inp.chain_name,
                },
                retry_count=3 if is_dead_letter else 0,
                error_message=(error_message if is_dead_letter else None),
                remediation_hint=hint if is_dead_letter else None,
                trace_id=str(
                    _chain_id(tenant_id, inp.chain_name, f"trace:{job_id}")
                ),
                **timestamps,
            )
        )
    await ctx.db.flush()

    dep_repo = JobDependencyRepository(ctx.db)
    parent = upstream_id
    for child in [root_id, *step_ids]:
        await dep_repo.add(child, [parent])
        parent = child
    await ctx.db.flush()

    logger.warning(
        "chaos create_stuck_dag injected",
        extra={
            "tenant_id": str(tenant_id),
            "chain_name": inp.chain_name,
            "root_job_id": str(root_id),
            "root_status": inp.root_status,
            "waiting_steps": inp.waiting_steps,
            "failed_step": inp.failed_step,
            "child_age_seconds": inp.child_age_seconds,
            "job_type": inp.job_type,
            "remediation_hint": hint,
        },
    )
    return CreateStuckDagOutput(
        root_job_id=str(root_id),
        completed_parent_id=str(upstream_id),
        waiting_job_ids=[str(sid) for sid in waiting_ids],
        step_job_ids=[str(sid) for sid in step_ids],
        dead_letter_job_id=(
            str(dead_letter_id) if dead_letter_id is not None else None
        ),
        chain_name=inp.chain_name,
        created=True,
        accepted=True,
    )


def _step_status(position: int, failed_step: int | None) -> str:
    """Status for descendant `position` (1-based) given the failed step.

    Rows ahead of the failed one are `completed`, because nothing dispatches
    before its parent finishes; the rest stay `waiting`.
    """
    if failed_step is None or position > failed_step:
        return JobStatus.WAITING.value
    if position == failed_step:
        return JobStatus.DEAD_LETTER.value
    return JobStatus.COMPLETED.value


def _assert_intact(
    chain_name: str,
    expected: dict[uuid.UUID, str],
    existing: Sequence[Job],
    tenant_id: uuid.UUID,
) -> None:
    """Idempotent repeat vs. drifted chain.

    Exactly the manufactured rows, in the caller's tenant, with exactly the
    manufactured statuses is a no-op. Anything else — partial, already
    remediated, longer than asked for, or the same `chain_name` in a different
    shape (ids ignore `root_status`, so a shape change reads as a status
    mismatch) — is refused. `existing` must cover the whole id space, which is
    what makes the extra-descendant arm reachable.
    """
    by_id = {j.id: j for j in existing}
    drift: list[str] = []
    for job_id, status in expected.items():
        row = by_id.get(job_id)
        if row is None:
            drift.append(f"{job_id}: missing")
        elif row.tenant_id != tenant_id:
            # Unreachable while ids are tenant-derived; only a non-RLS
            # session could see a foreign row here.
            drift.append(f"{job_id}: owned by another tenant")
        elif row.status != status:
            drift.append(f"{job_id}: {row.status!r} != {status!r}")
    for job_id in sorted(set(by_id) - set(expected), key=str):
        drift.append(
            f"{job_id}: unexpected extra descendant "
            f"(status {by_id[job_id].status!r}) — the stored chain is "
            "longer than the one requested"
        )
    if drift:
        raise CreateStuckDagError(
            f"chain_name {chain_name!r} is already in use and no longer "
            f"matches the manufactured chain ({'; '.join(drift)}). "
            "Pick a different chain_name or reset the environment; "
            "this hook never rewrites existing rows."
        )
