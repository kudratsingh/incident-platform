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

Why that chain is stuck by the platform's own rules, not by simulation:

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
  * `pause_dag(root_job_id)` is the stabilization the scenario grades:
    `get_dag_state` reads `paused=true` while descendants hold in
    `waiting`; the pause self-cleans on TTL.
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
fresh `chain_name` or reset the environment.

Chaos-only surface: gated behind `CHAOS_ENABLED=true` + `chaos:invoke`
scope + `environment_wide` blast radius label. See ADR 0008 gating.
Writes directly to `jobs` / `job_dependencies`; doesn't touch Kafka.
"""

import uuid
from collections.abc import Sequence

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext

# Deliberately reuses seed_dlq_messages' marker, owner fallback, hint
# validation, and canned error strings so the two declared-fixture
# hooks stay in lockstep — same disposal rule, same chaos-owner
# cleanup, same hint vocabulary (see that module's docstring).
from app.mcp.tools.chaos.seed_dlq_messages import (
    _DEFAULT_ERRORS,
    SEEDED_FIXTURE_MARKER,
    _fixture_owner,
    _validated_hint,
)
from app.models.enums import JobStatus, JobType
from app.models.job import Job
from app.repositories.job_dependency import JobDependencyRepository
from pydantic import BaseModel, ConfigDict, Field
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
        description="Error string on the dead-lettered root. Defaults "
        "to a realistic string matching the declared remediation_hint.",
    )


class CreateStuckDagOutput(BaseModel):
    root_job_id: str = Field(
        description="The dead-lettered job holding the chain — the id "
        "to alert on, probe with `get_dag_state`, pause, or replay."
    )
    completed_parent_id: str
    waiting_job_ids: list[str]
    chain_name: str
    created: bool = Field(
        description="False when the call was an idempotent repeat that "
        "found the chain already manufactured and intact."
    )
    accepted: bool


@chaos_tool(
    "create_stuck_dag",
    description=(
        "Manufacture a genuinely stuck dependency chain: a completed "
        "upstream parent, a dead-lettered root (retries exhausted), and "
        "N descendants held in `waiting` behind it. Stuck by the "
        "platform's own rules — the resolver promotes a child only when "
        "every parent is `completed`, and `dead_letter` is terminal — "
        "so the chain will not drain on its own. Observe with "
        "`get_dag_state(root_job_id)`. Compensators: `pause_dag` "
        "stabilizes (TTL self-cleans) and `replay_dlq_by_ids` on "
        "`root_job_id` genuinely unsticks the chain (the replayed root "
        "completes and the resolver promotes the descendants). Ids "
        "derive deterministically from the calling tenant and "
        "`chain_name`, so the same name in another tenant is a separate "
        "chain; rows are tagged as seeded fixtures and deleted by the "
        "next environment reset."
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
    expected: dict[uuid.UUID, str] = {
        upstream_id: JobStatus.COMPLETED.value,
        root_id: JobStatus.DEAD_LETTER.value,
        **{sid: JobStatus.WAITING.value for sid in step_ids},
    }

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
            waiting_job_ids=[str(sid) for sid in step_ids],
            chain_name=inp.chain_name,
            created=False,
            accepted=True,
        )

    user = await _fixture_owner(ctx, tenant_id)
    error_message = inp.error_message or _DEFAULT_ERRORS[hint]

    for job_id, status in expected.items():
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
                retry_count=3 if job_id == root_id else 0,
                error_message=(
                    error_message if job_id == root_id else None
                ),
                remediation_hint=hint if job_id == root_id else None,
                trace_id=str(
                    _chain_id(tenant_id, inp.chain_name, f"trace:{job_id}")
                ),
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
            "waiting_steps": inp.waiting_steps,
            "job_type": inp.job_type,
            "remediation_hint": hint,
        },
    )
    return CreateStuckDagOutput(
        root_job_id=str(root_id),
        completed_parent_id=str(upstream_id),
        waiting_job_ids=[str(sid) for sid in step_ids],
        chain_name=inp.chain_name,
        created=True,
        accepted=True,
    )


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
    remediated (root replayed, descendants promoted), or a chain that is
    *longer* than the one asked for — is refused: re-manufacturing would
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
