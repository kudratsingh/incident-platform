"""
`pause_dag_chaos` — set a DAG pause from the lab, indistinguishable from
an operator's.

Plan 01 §7.2 wants `resolver_stall` and `paused_dag` as a paired
comparison: identical node statuses, one boolean apart, opposite correct
answers. One of them says "nothing is coming for this child, escalate";
the other says "an operator paused this chain, wait or lift it". Nothing
could produce the second. `pause_dag` is the tool that sets the flag and
it declares `required_scope=actions:execute`, which the evaluator
principal deliberately does not hold (ADR 0012 § two principals,
WO-R3-187), and `ChaosHook` on the commander side refuses a non-chaos
tool name. So the world was unreachable by construction — not hard to
reach, unreachable. This hook closes that, and nothing else: it is the
same one-line Redis write, behind `chaos:invoke` instead of
`actions:execute`.

**What "indistinguishable" means here, exactly.** The flag is the
platform's own: `app/utils/dag_pause.pause_key_for(root)` →
`dag:paused:<root_id>`, value `"paused"`, with a TTL. That helper is
imported, not copied, so a rename cannot leave the lab writing a key the
resolver and `get_dag_state` no longer read. Because the value carries no
owner and `pause_state` answers off the key's presence and TTL,
`get_dag_state` returns the same `paused` / `paused_expires_in_seconds` /
`paused_by` triple whichever tool wrote it — there is no field for a lab
marker to hide in and none is invented (ADR 0012 rule 1 as amended to
cover response bodies; ADR 0029). The TTL default and bounds are
`pause_dag`'s own (600 s, 1..3600), so a lab pause taken with default
arguments is not merely shaped like an operator pause, it is the same
pause.

**The key is deliberately outside `chaos:*`.** Every other chaos hook
keeps its keys in that namespace and
`test_eval_reset.py::test_every_chaos_key_helper_lives_under_the_chaos_namespace`
holds them there, because `_clear_chaos_keys` is one `chaos:*` SCAN. This
hook cannot: a key named `chaos:…` would not be the key the resolver
reads, and the whole point is that the pause is real. Teardown is the
reset step that already existed for operator residue —
`reset_eval_state._clear_dag_pauses`, one `dag:paused:*` SCAN, reported
as `dag_pauses_cleared` — plus the TTL, which ends the pause with nothing
called.

**One thing the agent can tell apart, and it is an absence, not a name.**
An operator pause writes an `agent.tool_invoked` audit row; a chaos
invocation writes `chaos.tool_invoked`, which `list_audit_events` and
`get_trace` withhold from any principal without `chaos:invoke`
(WO-R3-187). So the agent sees *no* audit row for a lab pause where it
would see one for an operator's. That is the same property every chaos
hook has had since the token split, it leaks no lab vocabulary, and ADR
0029 records it rather than pretending otherwise.

Existence and tenancy are checked the way `pause_dag` checks them — same
`NotFoundError`, same message shape — so a scenario that pauses a job id
it mistyped is refused instead of silently succeeding against a key
nothing reads.

Sibling, not a replacement, of `pause_control_loop`. That one stops a
background **loop** with `chaos:pause:<loop>`; this one pauses one
dependency **DAG** with the platform's `dag:paused:<root>`. Pausing the
`resume_unblocked_waiting` loop and pausing a DAG look similar and are
opposites in the world: the loop pause makes a stranded child look like
nothing is coming for it, the DAG pause makes it look deliberately held.

Requires `chaos:invoke`. Registered only when `CHAOS_ENABLED=true` (see
`app/mcp/chaos.py`). Blast radius `environment_wide`: the honest label
for one DAG would be narrower than any member of that closed enum, and
this hook writes state into the shared world exactly as its sibling
`create_stuck_dag` does, which carries the same label. ADR 0029 records
the choice; the enum is not widened again.
"""

import uuid

from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext
from app.repositories.job import JobRepository
from app.utils.dag_pause import pause_key_for
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)

__all__ = ["pause_dag_chaos", "pause_key_for"]


class PauseDagChaosInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_job_id: uuid.UUID = Field(
        description="Root of the DAG to pause. Any WAITING child whose "
        "chain of parents reaches this root won't be promoted while the "
        "flag is set."
    )
    ttl_seconds: int = Field(
        default=600,
        ge=1,
        le=3600,
        description="How long the pause is active. Default 10 minutes — "
        "the same default and the same bounds the operator action uses, "
        "so a pause taken here with default arguments is identical to one "
        "an operator took.",
    )


class PauseDagChaosOutput(BaseModel):
    root_job_id: str
    pause_key: str
    ttl_seconds: int
    accepted: bool


@chaos_tool(
    "pause_dag_chaos",
    description=(
        "Pause promotion of WAITING children in the DAG rooted at "
        "`root_job_id`, producing a pause that is indistinguishable from "
        "an operator pause: the same Redis flag `pause_dag` writes, with a "
        "TTL, so `get_dag_state(root_job_id)` reads `paused: true` with "
        "`paused_expires_in_seconds`, a held descendant names this root as "
        "its `paused_by`, and children stay in status `waiting`. Nothing "
        "in what a reader gets back says the pause came from here. "
        "Self-cleans on TTL (default 600 s, max 3600), after which held "
        "children promote automatically, so the fault is bounded with "
        "nothing to call; the environment reset also clears every DAG "
        "pause. Exists because `pause_dag` needs `actions:execute`. Use "
        "`pause_control_loop` to stop a background loop instead — that is "
        "a different fault with the opposite meaning."
    ),
    input_model=PauseDagChaosInput,
    output_model=PauseDagChaosOutput,
    blast_radius=BlastRadius.ENVIRONMENT_WIDE,
)
async def pause_dag_chaos(
    inp: PauseDagChaosInput, ctx: ToolContext
) -> PauseDagChaosOutput:
    # Same check, same error, same message shape as the operator action:
    # a pause on a job that is missing or in a sibling tenant is a typo,
    # and a lab that silently accepted it would report a fault it did not
    # inject.
    job_repo = JobRepository(ctx.db)
    root = await job_repo.get_by_id(inp.root_job_id)
    if root is None or root.tenant_id != ctx.principal.tenant_id:
        raise NotFoundError(f"job not found: {inp.root_job_id}")

    key = pause_key_for(inp.root_job_id)
    # Byte-identical to `pause_dag`'s write. The value is never read as
    # anything but "present", and it must stay this string: an operator
    # inspecting Redis mid-run would otherwise find the lab in it.
    await ctx.redis.set(key, "paused", ex=inp.ttl_seconds)
    logger.warning(
        "chaos pause_dag_chaos injected",
        extra={
            "tenant_id": str(ctx.principal.tenant_id),
            "root_id": str(inp.root_job_id),
            "ttl_seconds": inp.ttl_seconds,
        },
    )
    return PauseDagChaosOutput(
        root_job_id=str(inp.root_job_id),
        pause_key=key,
        ttl_seconds=inp.ttl_seconds,
        accepted=True,
    )
