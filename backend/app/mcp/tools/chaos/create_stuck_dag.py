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
    cancels the descendants. Plain-DAG descendants of a dead-lettered
    parent stay `waiting` indefinitely. That is the platform's real
    stuck mode; this hook just declares an instance of it.

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

IDs derive from `uuid5(namespace, f"{chain_name}:{role}")` — same
deterministic-pinning convention as `scripts/seed_eval_fixtures.py`,
distinct namespace — so a scenario can pin the root id in YAML before
the hook ever runs. Re-invoking with the same `chain_name` while the
chain is intact is idempotent; once any row has drifted (someone
remediated it), the hook refuses rather than rewriting history — pick
a fresh `chain_name` or reset the environment.

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
# precompute the ids it pins: root = uuid5(ns, f"{chain_name}:root").
# Distinct from the eval seed's namespace so a chain can never collide
# with a boot-seeded fixture id.
_NAMESPACE = uuid.UUID("cccccccc-57ac-4000-8000-000000000000")


def _chain_id(chain_name: str, role: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"{chain_name}:{role}")


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
            "'{chain_name}:root') is the root id, ':upstream' the "
            "completed parent, ':step-1'..':step-N' the waiting "
            "descendants — so callers can pin ids before invoking. "
            "Re-invoking with the same name is idempotent while the "
            "chain is intact; refused once any row has drifted."
        ),
    )
    waiting_steps: int = Field(
        default=2,
        ge=1,
        le=10,
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
        "derive deterministically from `chain_name`; rows are tagged "
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

    upstream_id = _chain_id(inp.chain_name, "upstream")
    root_id = _chain_id(inp.chain_name, "root")
    step_ids = [
        _chain_id(inp.chain_name, f"step-{i}")
        for i in range(1, inp.waiting_steps + 1)
    ]
    expected: dict[uuid.UUID, str] = {
        upstream_id: JobStatus.COMPLETED.value,
        root_id: JobStatus.DEAD_LETTER.value,
        **{sid: JobStatus.WAITING.value for sid in step_ids},
    }

    existing = (
        (
            await ctx.db.execute(
                select(Job).where(Job.id.in_(list(expected)))
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
                trace_id=str(_chain_id(inp.chain_name, f"trace:{job_id}")),
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

    A repeat call that finds every row present, in the caller's tenant,
    with exactly the manufactured statuses is a no-op. Anything else —
    a partial chain, a sibling tenant's chain, or a chain someone has
    already remediated (root replayed, descendants promoted) — is
    refused: re-manufacturing would mean rewriting rows that are now
    history. The caller picks a fresh `chain_name` or resets the
    environment."""
    by_id = {j.id: j for j in existing}
    drift: list[str] = []
    for job_id, status in expected.items():
        row = by_id.get(job_id)
        if row is None:
            drift.append(f"{job_id}: missing")
        elif row.tenant_id != tenant_id:
            drift.append(f"{job_id}: owned by another tenant")
        elif row.status != status:
            drift.append(f"{job_id}: {row.status!r} != {status!r}")
    if drift:
        raise CreateStuckDagError(
            f"chain_name {chain_name!r} is already in use and no longer "
            f"matches the manufactured chain ({'; '.join(drift)}). "
            "Pick a different chain_name or reset the environment; "
            "this hook never rewrites existing rows."
        )
