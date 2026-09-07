"""The DAG tools describe the fix, not the pause (WO-R2-141).

Tool descriptions are the whole interface — the agent cannot read the
docstrings, the ADRs or this file, so a description that steers wrong is
a functional defect (CLAUDE.md, "Tool descriptions — normative"). These
four steered wrong together, and every layer agreed with them:

  * `get_dag_state` called itself "the verification surface for
    pause_dag", which is the reading a *working* pause produces on a
    chain that is still stuck.
  * `replay_dlq_by_ids` never mentioned DAG roots, though replaying the
    dead-lettered root is the platform's only un-stick path.
  * `pause_dag` described its own effect accurately and never said the
    effect expires, or that it blocks the replay while it holds.
  * `list_dlq_messages` never said it is the only read carrying a job's
    `remediation_hint`, which is what an agent needs before deciding a
    replay is safe.

Commander PR #191 countered all four in its planner prompt, in prose, as
a workaround — and wrote the durable deltas into its body for this repo
to apply. These are those deltas, pinned as claims rather than as a
whole-string snapshot: a snapshot of a 1500-character description
fails on every wording change and tells the next reader nothing about
which sentence mattered.

Rebless note: these strings are pinned by the commander's contract
snapshot, so they land at the next re-pin.
"""

import app.mcp.tools  # noqa: F401  — import fires every @tool decorator
import pytest
from app.mcp.registry import get_tool


def _description(tool_name: str) -> str:
    spec = get_tool(tool_name)
    assert spec is not None, f"{tool_name} is not registered"
    return spec.description


# --------------------------------------------------------------------------
# get_dag_state — verifies the fix as well as the pause
# --------------------------------------------------------------------------


def test_get_dag_state_is_not_advertised_as_the_pause_surface_alone() -> None:
    """The exact sentence that steered the agent toward pausing a chain
    it was graded for replaying. It must not come back."""
    text = _description("get_dag_state")
    assert "verification surface for pause_dag" not in text
    assert "verification surface for any change to the chain" in text


def test_get_dag_state_says_a_working_pause_leaves_the_chain_stuck() -> None:
    text = _description("get_dag_state")
    assert "a pause that WORKED and a chain that is still stuck" in text
    assert "self-expires" in text


def test_get_dag_state_describes_what_a_successful_replay_reads_as() -> None:
    """Without this the agent has a documented success signal for the
    pause and none for the fix."""
    text = _description("get_dag_state")
    assert "After `replay_dlq_by_ids` on a dead-lettered root" in text
    assert "no node in `dead_letter`" in text
    assert "descendants that were `waiting` promoted" in text


# --------------------------------------------------------------------------
# replay_dlq_by_ids — names the DAG-root case
# --------------------------------------------------------------------------


def test_replay_by_ids_names_the_dag_root_case() -> None:
    text = _description("replay_dlq_by_ids")
    assert "DAG ROOTS:" in text
    assert "un-stick path for a stalled chain" in text


def test_replay_by_ids_explains_why_replaying_the_root_works() -> None:
    """The mechanism, not just the instruction — it is what lets the
    agent verify its own plan instead of trusting the description."""
    text = _description("replay_dlq_by_ids")
    assert "`dead_letter` is terminal" in text
    assert "only when every parent is `completed`" in text
    assert "get_dag_state(root_job_id)" in text


def test_replay_by_ids_warns_that_a_pause_blocks_it() -> None:
    text = _description("replay_dlq_by_ids")
    assert "refused while any ancestor is paused" in text
    assert "do not pause a chain you intend to replay" in text


# --------------------------------------------------------------------------
# pause_dag — a stabilizer that says so
# --------------------------------------------------------------------------


def test_pause_dag_says_it_is_not_a_fix() -> None:
    text = _description("pause_dag")
    assert "STABILIZER, NOT A FIX:" in text
    assert "changes nothing about the node that stopped the chain" in text


def test_pause_dag_says_the_stall_returns_when_the_ttl_lapses() -> None:
    text = _description("pause_dag")
    assert "promote back into the same stalled state" in text


def test_pause_dag_says_it_blocks_the_remedy_while_it_holds() -> None:
    text = _description("pause_dag")
    assert "refuses to replay any job inside a paused DAG" in text
    assert "never as a remediation" in text


# --------------------------------------------------------------------------
# list_dlq_messages — the only read carrying a hint
# --------------------------------------------------------------------------


def test_list_dlq_says_it_is_the_only_read_exposing_a_hint() -> None:
    text = _description("list_dlq_messages")
    assert "ONLY read that exposes a job's `remediation_hint`" in text
    assert "`get_dag_state` does not carry it" in text


def test_list_dlq_says_a_dag_root_appears_like_any_other_row() -> None:
    text = _description("list_dlq_messages")
    assert "dead-lettered DAG root appears in this listing" in text


def test_list_dlq_states_the_cost_of_finding_one_row() -> None:
    """Honest about a missing filter rather than silent about it —
    WO-R2-142 is the feature request that would remove the cost."""
    text = _description("list_dlq_messages")
    assert "no job-id filter" in text
    assert "paging or filtering by category" in text


# --------------------------------------------------------------------------
# The pair reads consistently from both ends
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool_name", "counterpart"),
    [
        ("get_dag_state", "replay_dlq_by_ids"),
        ("replay_dlq_by_ids", "get_dag_state"),
        ("pause_dag", "replay"),
    ],
)
def test_each_dag_tool_names_the_tool_on_the_other_side(
    tool_name: str, counterpart: str
) -> None:
    """The reciprocal guidance used to live only in `create_stuck_dag` —
    a chaos tool the agent is never shown. Each half now names the
    other on a surface the agent actually reads."""
    assert counterpart in _description(tool_name)


def test_no_schema_changed_with_these_deltas() -> None:
    """Descriptions are contract, but this change is description-only:
    a shape change would be a different rebless class and would need a
    commander-side code change, not just a re-record."""
    for name, fields in (
        ("get_dag_state", {"job_id"}),
        ("pause_dag", {"root_job_id", "ttl_seconds", "idempotency_key"}),
        (
            "list_dlq_messages",
            {"job_type", "remediation_hint", "limit", "offset"},
        ),
        (
            "replay_dlq_by_ids",
            {"job_ids", "delay_seconds", "idempotency_key"},
        ),
    ):
        spec = get_tool(name)
        assert spec is not None
        properties = set(spec.input_model.model_json_schema()["properties"])
        assert properties == fields, (name, properties)
