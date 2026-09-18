"""
`create_stuck_dag` — manufacture a dependency chain that is genuinely
stuck and stays stuck until remediated.

The `remediate_runaway_saga_success` scenario needs a DAG that is not
making progress at probe time. The boot-seeded three-node DAG cannot
provide one: its parent is `completed`, so the resolver (or the resume
sweep) promotes the whole chain within seconds of boot and the fault
evaporates before the agent ever probes it. This hook builds the chain
the scenario actually describes:

    upstream (completed) → root (dead_letter) → N descendants (waiting)

## Three chain shapes, one hook (WO-R3-274, ADR 0029)

`root_status` picks between two, and `failed_step` splits the second.
The default is the chain above, byte for byte:

  * `root_status="dead_letter"` (default) — the chain above. Stuck by
    the platform's own rules; see the next section.
  * `root_status="completed"` — upstream completed → root **completed**
    → step-1..N `waiting`, and **no dead-letter row anywhere**. Plan 01
    §7.2's `resolver_stall` world: the discriminator is an absence, so
    the correct answer is to escalate rather than to replay something.
  * `root_status="completed"` + `failed_step=K` — the same, except
    descendant K is `dead_letter` and the K-1 descendants ahead of it are
    `completed`, because a job cannot have been dispatched before its own
    parent finished. Plan 01 §7.2's `downstream_child_failed`: the root
    is fine and something further down is not.

**The completed-root shapes are not self-sustaining, and the description
says so.** With the root `completed`, step-1 has no unmet parent, so both
promoters will drain it: the `dependency-resolver` consumer group on the
next `job.completed` redelivery, and `_resume_unblocked_waiting_loop`
within ~10 s regardless. Stranding that child takes both stalls —
`kill_consumer('dependency-resolver')` and
`pause_control_loop('resume_unblocked_waiting')` — which is the finding
of WO-R3-213 and the 2026-09-17 amendment to ADR 0027. This hook writes
the rows; those two hooks keep them. Advertising a "stuck" chain that
quietly drains would be the fourth description rule's exact failure
(CLAUDE.md: never advertise a safety property the tool cannot deliver),
so the description names both companions. The `failed_step` shape is the
exception and needs neither: no `waiting` row in it has a `completed`
parent (K-1 of them are themselves `completed`, and everything behind K
waits on a `dead_letter`, which is terminal), so it is stuck the same way
the default chain is.

Why the default chain is stuck by the platform's own rules, not by
simulation:

  * `DependencyResolver` promotes a WAITING child only when
    `unmet_count == 0` — every parent `completed`
    (`app/workers/dependency_resolver.py`). The resume sweep applies
    the same gate.
  * `dead_letter` is terminal. Nothing retries it — the delayed-retry
    loop and the LLM retry policy act before dead-letter, the stale
    sweeps act on PENDING/RUNNING — so the root never emits the
    `job.completed` the descendants are waiting for.
  * The chain carries no `saga_id`, so the saga coordinator never
    cancels the descendants.

That last point used to read "plain-DAG descendants of a dead-lettered
parent stay `waiting` indefinitely — the platform's real stuck mode".
Since #165 that is true only of *directly-inserted* rows, which is what
this hook writes. `JobRepository.update_status` now cascades CANCELLED
to non-saga WAITING descendants when a parent *transitions* into
DEAD_LETTER or CANCELLED — ADR 0022 §3,
`docs/ADR/0022-promotable-only-resume-sweep-and-dependency-cascade.md`
— so a chain that reached this shape by transition would drain
itself. This hook inserts the terminal status rather than transitioning
into it, so the cascade never fires on it — a genuine property of where
that chokepoint sits, not luck. The manufactured state is therefore
still stuck by the platform's rules, but it is a state the platform no
longer *produces* on its own; treat it as a declared instance of the
pre-#165 stuck mode rather than as a sample of live behaviour.

Compensating actions (ADR 0008 amendment — named on both sides):

  * `replay_dlq_by_ids` on `root_job_id` genuinely unsticks the chain:
    replay resets the root to `pending`, the dispatcher completes it,
    and the resolver promotes each descendant in turn. Round-trip test:
    `test_create_stuck_dag_round_trip_with_replay_dlq_by_ids` in
    `tests/api/test_mcp_chaos_stuck_dag.py`.
  * `pause_dag(root_job_id)` stabilizes without fixing: `get_dag_state`
    reads `paused=true` while descendants hold in `waiting`, and the
    pause self-cleans on TTL — so the chain is stuck again the moment it
    lapses, and while it holds the platform refuses the replay above.
    The scenario grades the replay, not the pause (commander ADR 0026 —
    a stabilizer is not a resolution). This bullet used to call the
    pause "the stabilization the scenario grades", which stopped being
    true when that scenario was redesigned around the replay.

The dead-lettered row's `error_message` is derived from the
`remediation_hint` the call declares, through
`app.lab.dlq_failure_stories`. The two have to agree: the agent reads
that row before deciding whether a replay is safe, and a `replay_safe`
root whose text named a permanent schema violation is what made it
escalate on a scenario graded for a replay (WO-R2-146). "That row" is
the root under the default, descendant `failed_step` when one is asked
for, and **nothing at all** under a bare `root_status="completed"` — a
chain with no dead-letter row has nothing to carry a hint, so both
fields go unused rather than being stamped somewhere they would read as
a fault the world does not contain.
  * Every row is tagged `payload.seeded_fixture = true`, so the reset
    sweep (`scripts/reset_eval_state.py::_delete_seeded_dlq_fixtures`)
    DELETEs the whole chain — edges CASCADE with the jobs (ADR 0012
    rule 2 disposal). A demo stack can never be left permanently
    wedged: replay drains the chain, and reset deletes it.

IDs derive from `uuid5(namespace, f"{tenant_id}:{chain_name}:{role}")`
— same deterministic-pinning convention as
`scripts/seed_eval_fixtures.py`, distinct namespace — so a scenario can
pin the root id in YAML before the hook ever runs, given the tenant it
will run as. The tenant id is in the key deliberately: the idempotency
probe below runs on the RLS-scoped MCP session, so a chain another
tenant manufactured under the same `chain_name` would be *invisible* to
it and the hook would fall through to an INSERT that collides on the
primary key — a 500 where the contract promises a 409. Widening the
probe past RLS to see that row would be the wrong repair; making the id
space per-tenant means the collision cannot be represented at all, and
two tenants can drill the same `chain_name` concurrently.

Re-invoking with the same `chain_name` while the chain is intact is
idempotent; once any row has drifted (someone remediated it) — or the
stored chain is *longer* than the one now being asked for — the hook
refuses rather than rewriting history or under-reporting it. Pick a
fresh `chain_name` or reset the environment. Note what that means across
shapes: the ids do not depend on `root_status`, so asking for a
*different* shape under a `chain_name` that already exists is drift, not
a repeat, and is refused with `stuck_chain_name_in_use` — the statuses
do not match. That is the intended reading; rewriting a chain's shape
under its own name would silently change the world a scenario already
pinned.

`child_age_seconds` backdates `created_at`/`updated_at` on **every** row
in the chain, not only the descendants. Plan 01 §7.2 reads "child
created_at age large" and the platform's own proof used 47 minutes
(`tests/integration/test_resolver_stall.py`), which is what the field is
named for — but moving only the descendants would make them older than
the parents they depend on, a graph the platform cannot produce. Dispatch
timestamps (`started_at` / `completed_at`) are left NULL on every row,
exactly as they already were, so the backdate is one column pair and not
a second lifecycle to keep coherent.

Chaos-only surface: gated behind `CHAOS_ENABLED=true` + `chaos:invoke`
scope + `environment_wide` blast radius label. See ADR 0008 gating.
Writes directly to `jobs` / `job_dependencies`; doesn't touch Kafka.
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

# Deliberately reuses seed_dlq_messages' marker, owner fallback, and hint
# validation so the two declared-fixture hooks stay in lockstep — same
# disposal rule, same chaos-owner cleanup, same hint vocabulary (see that
# module's docstring). The canned error strings are shared too, but they
# now come from `app.lab.dlq_failure_stories` rather than from a dict in
# that module.
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

# uuid5 namespace for chain ids. Fixed and documented so a scenario can
# precompute the ids it pins:
# root = uuid5(ns, f"{tenant_id}:{chain_name}:root").
# Distinct from the eval seed's namespace so a chain can never collide
# with a boot-seeded fixture id.
_NAMESPACE = uuid.UUID("cccccccc-57ac-4000-8000-000000000000")

# Upper bound on `waiting_steps`, and therefore on the chain's whole id
# space. One constant because the integrity probe has to enumerate every
# id this hook could *ever* have written for a chain_name — not just the
# ones the current call wants — to notice descendants left by a longer
# earlier call. Raising the field bound without raising this one would
# blind the probe to the new tail.
_MAX_WAITING_STEPS = 10

# Upper bound on `child_age_seconds` — one day. Bounded for the same reason
# every other lab dial here is: an unbounded backdate puts a fixture outside
# `search_traces(since_hours=...)`'s widest window (168 h) and outside any age
# an operator would believe of a chain on a stack that is usually hours old.
# A day is comfortably more than the 47 minutes the platform's own stranded-
# child proof uses.
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
        one chain and no world the plan asks for; and a `failed_step` past the
        end of the chain would silently address a descendant this call is not
        creating. Both are argument errors (JSON-RPC invalid params), not
        `stuck_chain_name_in_use` — nothing about the environment is wrong.

        `PydanticCustomError`, not a bare `ValueError`, for the reason
        `saturate_redis._bound_total_footprint` gives: the MCP handler returns
        `exc.errors()` as the invalid-params payload and json-encodes it, and
        pydantic puts the raised *exception object* in `ctx` for a plain
        `ValueError` — which is not serializable, so the refusal would leave as
        a 500 instead of the invalid params it is. A custom error's ctx is the
        dict passed here.
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

    # Probe the chain's *whole* id space, not just the rows this call
    # wants, so a longer chain left by an earlier call is visible as
    # extra descendants instead of being silently omitted from the
    # response (R2-55). Still a primary-key IN over at most 12 ids.
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
    # Derived from the hint this call declares, never fixed: the row the
    # agent reads has to describe a failure the declared hint's action
    # would actually fix. A chain seeded `replay_safe` whose root said
    # SchemaValidationError is what made the agent escalate, correctly,
    # on a scenario graded for a replay (WO-R2-146).
    error_message = inp.error_message or default_error_for(hint)

    # `created_at`/`updated_at` are only passed when a backdate was asked
    # for, so the default call writes exactly the rows it always wrote and
    # lets the server default stamp them.
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

    With no `failed_step` every descendant is `waiting`. With one, the chain
    reads as a run that got as far as N and stopped there: the descendants
    ahead of it must be `completed`, because the platform cannot dispatch a
    job whose parent has not finished, and the ones behind it are `waiting`
    on a row that will never complete.
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

    A repeat call that finds exactly the manufactured rows present, in
    the caller's tenant, with exactly the manufactured statuses is a
    no-op. Anything else — a partial chain, a chain someone has already
    remediated (root replayed, descendants promoted), a chain that is
    *longer* than the one asked for, or a chain built under this
    `chain_name` in a *different shape* (the ids do not depend on
    `root_status` or `failed_step`, so a shape change reads here as a
    status mismatch, which is exactly right) — is refused: re-manufacturing would
    mean rewriting rows that are now history, and returning `intact`
    would mean under-reporting the chain the caller then reasons about.
    The caller picks a fresh `chain_name` or resets the environment.

    `existing` must cover the chain's whole id space, not just
    `expected`; that is what makes the extra-descendant arm reachable.
    """
    by_id = {j.id: j for j in existing}
    drift: list[str] = []
    for job_id, status in expected.items():
        row = by_id.get(job_id)
        if row is None:
            drift.append(f"{job_id}: missing")
        elif row.tenant_id != tenant_id:
            # Unreachable while ids are tenant-derived; kept because it
            # is the invariant the derivation exists to guarantee, and a
            # non-RLS session (a script, a superuser) is the one caller
            # that could still see a foreign row here.
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
