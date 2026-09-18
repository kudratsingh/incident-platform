"""A stranded chain and a lab pause are manufactured, not found (WO-R3-274/275).

Family C (plan 01 §7.2) needs five worlds and three of them could not be built.
Two mechanisms close that, and this file pins the parts of them that are claims
rather than behaviour — schemas, descriptions, refusals, key identity, and the
two documentation promises the reset now makes.

  * `create_stuck_dag` gains `root_status`, `child_age_seconds` and
    `failed_step`. The default is byte-for-byte the old chain (upstream
    completed → root dead-lettered → N waiting). `root_status="completed"`
    produces the `resolver_stall` shape: no dead-letter row anywhere.
    `failed_step` produces `downstream_child_failed`: exactly one.
  * `pause_dag_chaos` writes the flag `pause_dag` writes, with a TTL, so
    `get_dag_state` reads `paused: true` for a principal that never had
    `actions:execute`. The point of the hook is that the flag is
    *indistinguishable* from an operator's, so the identity is asserted against
    `pause_dag`'s own helper and value rather than against a copy of them.

Descriptions are pinned as claims, not whole-string snapshots — same convention
as `test_poison_and_mislabel_tool_descriptions.py`: a snapshot of a 2000-char
description fails on every wording change and says nothing about which sentence
mattered.

Rebless note: the enumerated deltas for the next re-pin are at the bottom of
this file, and `CLAUDE.md`'s "Release ordering" bullet cites them.
"""

from __future__ import annotations

import importlib
import inspect
import pathlib
import re
import sys
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import app.mcp.tools  # noqa: F401  — import fires every @tool decorator
import pytest
from app.config import Settings
from app.core.scopes import Scope
from app.mcp.chaos import BlastRadius
from app.mcp.registry import (
    _restore_for_tests,
    _snapshot_for_tests,
    get_tool,
    list_tools,
)
from app.models.enums import JobStatus
from app.utils.dag_pause import pause_key_for as dag_pause_key_for
from pydantic import ValidationError

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

# `scripts/` is not a package on disk; make it importable flat, the way
# `test_eval_reset.py` does.
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))

_CHAOS_MODULES = ("create_stuck_dag", "pause_dag_chaos")


@pytest.fixture
def chaos_registered() -> Iterator[None]:
    """Both hooks are chaos-gated, so under the unit tier's default
    `CHAOS_ENABLED=false` neither is in the registry and neither has a schema to
    assert on (ADR 0008 gate 1). Reload them under patched settings, then
    restore — the trick `test_chaos_gating.py` established."""
    from app.mcp import chaos as chaos_mod
    from app.mcp.tools import chaos as chaos_pkg

    modules = [getattr(chaos_pkg, name) for name in _CHAOS_MODULES]
    snapshot = _snapshot_for_tests()
    try:
        with patch.object(
            chaos_mod,
            "get_settings",
            return_value=Settings(chaos_enabled=True, environment="test"),
        ):
            for module in modules:
                importlib.reload(module)
        yield
    finally:
        _restore_for_tests(snapshot)
        for module in modules:
            importlib.reload(module)


@pytest.fixture
def whole_chaos_surface_registered() -> Iterator[None]:
    """Every chaos hook registered, for the one test that counts the surface.

    `chaos_registered` reloads this packet's two modules only, which is right
    for a schema assertion and useless for a count — the other ten hooks are
    absent from the registry under the unit tier's `CHAOS_ENABLED=false`.
    """
    from app.mcp import chaos as chaos_mod
    from app.mcp.tools import chaos as chaos_pkg

    modules = [
        getattr(chaos_pkg, name)
        for name in dir(chaos_pkg)
        if not name.startswith("_") and hasattr(getattr(chaos_pkg, name), "__file__")
    ]
    snapshot = _snapshot_for_tests()
    try:
        with patch.object(
            chaos_mod,
            "get_settings",
            return_value=Settings(chaos_enabled=True, environment="test"),
        ):
            for module in modules:
                importlib.reload(module)
        yield
    finally:
        _restore_for_tests(snapshot)
        for module in modules:
            importlib.reload(module)


def _description(tool_name: str) -> str:
    spec = get_tool(tool_name)
    assert spec is not None, f"{tool_name} is not registered"
    return spec.description


def _field_description(tool_name: str, field: str) -> str:
    spec = get_tool(tool_name)
    assert spec is not None, f"{tool_name} is not registered"
    schema = spec.input_model.model_json_schema()
    described = schema["properties"][field].get("description", "")
    assert described, f"{tool_name}.{field} has no description"
    return str(described)


def _input(tool_name: str, **kwargs: Any) -> Any:
    spec = get_tool(tool_name)
    assert spec is not None, f"{tool_name} is not registered"
    return spec.input_model(**kwargs)


# ---------------------------------------------------------------------------
# create_stuck_dag — the three new inputs, and what they promise
# ---------------------------------------------------------------------------


def test_root_status_defaults_to_the_chain_this_hook_always_built(
    chaos_registered: None,
) -> None:
    """The default has to stay the old world exactly, because four scenarios
    and the commander's canned fixtures are graded against it."""
    inp = _input("create_stuck_dag")
    assert inp.root_status == "dead_letter"
    assert inp.child_age_seconds == 0
    assert inp.failed_step is None


def test_root_status_is_a_closed_two_member_literal(
    chaos_registered: None,
) -> None:
    """A third shape would be a third world, and every world in this family is
    a decision. Anything else is refused by Pydantic before the handler runs —
    invalid params, not a chain nobody asked for."""
    spec = get_tool("create_stuck_dag")
    assert spec is not None
    schema = spec.input_model.model_json_schema()
    assert schema["properties"]["root_status"]["enum"] == [
        "dead_letter",
        "completed",
    ]
    with pytest.raises(ValidationError):
        _input("create_stuck_dag", root_status="waiting")


def test_the_description_says_the_completed_chain_carries_no_dlq_row(
    chaos_registered: None,
) -> None:
    """The discriminator of the `resolver_stall` world is an absence, so the
    absence is what the description has to promise."""
    text = _description("create_stuck_dag")
    assert "no dead-letter row anywhere in the chain" in text


def test_the_description_says_the_completed_chain_is_not_self_sustaining(
    chaos_registered: None,
) -> None:
    """The honesty rule with teeth (CLAUDE.md, fourth description rule: never
    advertise a safety property the tool cannot deliver).

    The dead-lettered default is stuck by the platform's own rules. The
    completed variant is NOT: the root is `completed`, so step-1 has no unmet
    parent and both promoters — the `dependency-resolver` consumer group and
    the resume sweep — will drain it within about ten seconds. A description
    that called this chain "stuck" without saying so would be advertising a
    fault that evaporates before the agent probes it, which is the exact defect
    this hook was created to fix in the first place.
    """
    text = _description("create_stuck_dag")
    assert "does NOT hold by itself" in text
    assert "kill_consumer" in text
    assert "pause_control_loop" in text
    assert "resume_unblocked_waiting" in text


def test_the_description_says_which_row_failed_step_dead_letters(
    chaos_registered: None,
) -> None:
    text = _description("create_stuck_dag")
    assert "exactly one dead-letter row" in text
    described = _field_description("create_stuck_dag", "failed_step")
    # The coherence refinement: a job cannot have run before its own parent,
    # so the descendants ahead of the failed one are `completed`.
    assert "descendants before it are `completed`" in described
    assert "`root_status=\"completed\"`" in described


def test_the_description_says_the_backdate_covers_the_whole_chain(
    chaos_registered: None,
) -> None:
    """`child_age_seconds` is named for the fact a scenario reads ("child
    created_at age large"), but it moves the parents too — a child that is
    older than its own parent is a graph the platform cannot produce."""
    described = _field_description("create_stuck_dag", "child_age_seconds")
    assert "every row in the chain" in described
    assert "older than its own parent" in described


def test_failed_step_without_a_completed_root_is_refused(
    chaos_registered: None,
) -> None:
    """The two inputs are not independent: a dead-lettered root plus a
    dead-lettered descendant is two faults in one chain and no world in the
    plan. Refused as invalid params, before any row is written."""
    with pytest.raises(ValidationError) as excinfo:
        _input("create_stuck_dag", failed_step=1)
    assert "root_status" in str(excinfo.value)


def test_failed_step_past_the_end_of_the_chain_is_refused(
    chaos_registered: None,
) -> None:
    with pytest.raises(ValidationError):
        _input(
            "create_stuck_dag",
            root_status="completed",
            waiting_steps=2,
            failed_step=3,
        )
    # …and the last descendant is fine.
    assert (
        _input(
            "create_stuck_dag",
            root_status="completed",
            waiting_steps=2,
            failed_step=2,
        ).failed_step
        == 2
    )


def test_child_age_seconds_is_bounded(chaos_registered: None) -> None:
    """Bounded like every other lab dial here: an unbounded backdate can put a
    fixture outside `search_traces(since_hours=...)`' widest window (168h) and
    outside any age an operator would believe."""
    assert _input("create_stuck_dag", child_age_seconds=86_400)
    with pytest.raises(ValidationError):
        _input("create_stuck_dag", child_age_seconds=86_401)
    with pytest.raises(ValidationError):
        _input("create_stuck_dag", child_age_seconds=-1)


# ---------------------------------------------------------------------------
# pause_dag_chaos — the flag is the operator's flag
# ---------------------------------------------------------------------------


def test_lab_pause_is_not_registered_when_chaos_is_disabled() -> None:
    """Gate 1, asserted with no fixture: the unit tier's default settings."""
    from app.mcp.tools.chaos import pause_dag_chaos  # noqa: F401

    assert "pause_dag_chaos" not in {t.name for t in list_tools()}


def test_lab_pause_requires_chaos_invoke_and_declares_a_blast_radius(
    chaos_registered: None,
) -> None:
    spec = get_tool("pause_dag_chaos")
    assert spec is not None
    assert spec.required_scope == Scope.CHAOS_INVOKE
    assert spec.is_chaos is True
    # No new BlastRadius member (the enum moved once already, for
    # `pause_control_loop`). Every hook that writes state into the shared world
    # carries `environment_wide`, and this one's sibling `create_stuck_dag`
    # does; see ADR 0029.
    assert spec.description.startswith(
        f"[chaos: {BlastRadius.ENVIRONMENT_WIDE.value}] "
    )


def test_lab_pause_writes_the_same_key_the_operator_pause_writes() -> None:
    """The whole design in one assertion. Not a copy of the key format — the
    shipped helper, so a rename cannot leave the lab writing a key the resolver
    and `get_dag_state` no longer read."""
    from app.mcp.tools.actions import pause_dag as action_module
    from app.mcp.tools.chaos import pause_dag_chaos as hook_module

    assert hook_module.pause_key_for is dag_pause_key_for
    assert action_module.pause_key_for is dag_pause_key_for


def test_lab_pause_writes_the_same_value_the_operator_pause_writes() -> None:
    """The value is read by nothing but `EXISTS`/`TTL`, which is exactly why it
    must not be allowed to drift into lab vocabulary: `pause_state` returns
    `paused=true` off the key's presence, and a value like `"chaos"` would sit
    in Redis for any operator with `redis-cli` to find while the agent is being
    graded on whether the pause looks real (ADR 0012, and ADR 0029).

    Read out of both modules' source so the two literals cannot diverge without
    this failing.
    """
    from app.mcp.tools.actions import pause_dag as action_module
    from app.mcp.tools.chaos import pause_dag_chaos as hook_module

    pattern = re.compile(r"""redis\.set\(\s*key,\s*(["'][^"']+["'])""")
    operator = pattern.search(inspect.getsource(action_module.pause_dag))
    lab = pattern.search(inspect.getsource(hook_module.pause_dag_chaos))
    assert operator is not None and lab is not None
    assert operator.group(1) == lab.group(1) == '"paused"'


def test_lab_pause_ttl_bounds_match_the_operator_pause(
    chaos_registered: None,
) -> None:
    """Same bounds AND the same default, so a lab pause taken with default
    arguments is not merely shaped like an operator pause — it is the same
    pause. `pause_control_loop` fixed the bounds at 1..3600 and `pause_dag`
    defaults to ten minutes; both are honoured here."""
    from app.mcp.tools.actions.pause_dag import PauseDagInput

    spec = get_tool("pause_dag_chaos")
    assert spec is not None
    lab = spec.input_model.model_fields["ttl_seconds"]
    operator = PauseDagInput.model_fields["ttl_seconds"]
    assert lab.default == operator.default == 600
    for field in (lab, operator):
        bounds = {
            type(m).__name__: getattr(m, "ge", getattr(m, "le", None))
            for m in field.metadata
        }
        assert bounds == {"Ge": 1, "Le": 3600}


def test_lab_pause_takes_no_idempotency_key(chaos_registered: None) -> None:
    """The one field it deliberately does not copy. `Idempotency-Key` is the
    Tier-1 action contract (`app/mcp/handlers.py` claims it before the action
    runs); a chaos hook is not a Tier-1 action, and no other hook takes one.
    Setting the same key twice is idempotent in Redis anyway."""
    spec = get_tool("pause_dag_chaos")
    assert spec is not None
    assert set(spec.input_model.model_fields) == {"root_job_id", "ttl_seconds"}


def test_lab_pause_description_says_it_is_shaped_like_an_operator_pause(
    chaos_registered: None,
) -> None:
    text = _description("pause_dag_chaos")
    assert "indistinguishable from an operator pause" in text
    assert "`get_dag_state(root_job_id)`" in text
    assert "paused: true" in text
    assert "Self-cleans on TTL" in text
    # And why it exists at all, so a reader does not take it for a duplicate.
    assert "`pause_dag` needs `actions:execute`" in text


def test_neither_hook_puts_a_docstring_on_a_schema_bearing_model(
    chaos_registered: None,
) -> None:
    """A class docstring on a Pydantic model is serialized as the schema's
    `description`, and both of these schemas reach `tools/list` — so a docstring
    here is a contract change disguised as a comment (workspace rule; the same
    reason `GetDagStateOutput.seed_id` carries an explicit title)."""
    for tool_name in ("create_stuck_dag", "pause_dag_chaos"):
        spec = get_tool(tool_name)
        assert spec is not None
        for model in (spec.input_model, spec.output_model):
            schema = model.model_json_schema()
            assert "description" not in schema, (
                f"{tool_name}: {model.__name__} has a class docstring, which "
                "lands in the pinned tool schema"
            )


# ---------------------------------------------------------------------------
# Teardown — the pause is swept by the step that already existed
# ---------------------------------------------------------------------------


def _reset_source() -> str:
    return (_REPO_ROOT / "scripts" / "reset_eval_state.py").read_text(
        encoding="utf-8"
    )


def test_the_reset_pattern_that_clears_dag_pauses_covers_the_lab_pause() -> None:
    """`_clear_dag_pauses` is the teardown, and it already existed — but it
    existed for *agent* residue, so nothing said it was also the chaos
    teardown for this hook.

    Asserted as a match of the shipped key against the shipped pattern rather
    than as a string comparison, because the point is coverage: a chaos hook
    whose keys escape `chaos:*` escapes `_clear_chaos_keys`, and this is the
    one hook that does that deliberately.
    """
    import fnmatch
    import uuid as _uuid

    reset = importlib.import_module("reset_eval_state")
    key = dag_pause_key_for(_uuid.uuid4())
    assert not fnmatch.fnmatch(key, "chaos:*"), (
        "the lab pause is supposed to look like an operator pause, so it must "
        "NOT be under chaos:* — see ADR 0029"
    )
    assert fnmatch.fnmatch(key, "dag:paused:*")
    assert "dag:paused:*" in inspect.getsource(reset._clear_dag_pauses)
    assert "dag_pauses_cleared" in _reset_source()


class _NullTx:
    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return None

    async def __aexit__(self, *_a):  # type: ignore[no-untyped-def]
        return False


class _SessionProxy:
    """`begin()` is a no-op because the `db_session` fixture owns the
    transaction; everything else — including `bind`, which the reset's dialect
    branch reads — forwards. Same shim `test_eval_reset.py` uses."""

    def __init__(self, session: Any) -> None:
        self._s = session

    def begin(self) -> _NullTx:
        return _NullTx()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._s, name)

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False


async def test_the_reset_deletes_a_whole_stranded_chain(
    db_session: Any,
    default_tenant: Any,
    test_user: Any,
) -> None:
    """The disposal guarantee, on the shape that has no dead-letter row in it.

    `_delete_seeded_dlq_fixtures` is named for the DLQ and matches on the
    `seeded_fixture` payload marker rather than on a status (ADR 0012 rule 2),
    which is exactly why a chain with nothing dead-lettered is still swept — but
    nothing asserted it, and "the DLQ sweep" is a name that invites the
    assumption that it needs a `dead_letter` row. Every row of the chain carries
    the marker, so every row goes; a real user's job beside it does not. Edges
    go with the jobs by FK CASCADE, which `tests/integration/
    test_eval_reset_postgres.py` covers against the server that enforces it.
    """
    from app.core.scopes import Scope as _Scope
    from app.dependencies import Principal
    from app.mcp.registry import ToolContext
    from app.mcp.tools.chaos.create_stuck_dag import (
        CreateStuckDagInput,
        create_stuck_dag,
    )
    from app.models.job import Job
    from app.models.job_dependency import JobDependency
    from sqlalchemy import func, select

    reset = importlib.import_module("reset_eval_state")

    ctx = ToolContext(
        db=db_session,
        redis=None,
        principal=Principal(
            kind="service_account",
            tenant_id=default_tenant.id,
            scopes=frozenset({_Scope.CHAOS_INVOKE.value}),
        ),
    )
    made = await create_stuck_dag(
        CreateStuckDagInput(
            chain_name="disposable",
            root_status="completed",
            waiting_steps=3,
            child_age_seconds=600,
        ),
        ctx,
    )
    chain = [
        made.root_job_id,
        made.completed_parent_id,
        *made.step_job_ids,
    ]
    bystander = Job(
        id=uuid.uuid4(),
        tenant_id=default_tenant.id,
        user_id=test_user.id,
        type="csv_upload",
        status=JobStatus.COMPLETED.value,
        payload={"rows": 1},
    )
    db_session.add(bystander)
    await db_session.flush()

    assert (
        await db_session.execute(
            select(func.count()).select_from(JobDependency)
        )
    ).scalar_one() == 4  # root + three steps, one edge each

    deleted = await reset._delete_seeded_dlq_fixtures(
        lambda: _SessionProxy(db_session)
    )
    assert deleted == len(chain)

    survivors = {
        str(row_id)
        for row_id in (
            await db_session.execute(select(Job.id))
        ).scalars()
    }
    assert survivors == {str(bystander.id)}


def test_the_reset_docstring_names_the_lab_pause_as_one_of_its_sources() -> None:
    """The docstring is the operating manual for the reset and it attributed
    every `dag:paused:*` flag to the agent. One is now the lab's, and a reader
    deciding whether a leftover pause means the agent acted needs that."""
    text = _reset_source()
    assert "pause_dag_chaos" in text


# ---------------------------------------------------------------------------
# The drained trio — item 3: make the spec honest, not the reset racy
# ---------------------------------------------------------------------------


def test_the_reset_says_the_seeded_dag_trio_is_drained_after_first_boot() -> None:
    """The latent defect, documented rather than "fixed".

    `dag-seed-job` and `dag-child-job` are seeded `WAITING` behind a
    `COMPLETED` parent, so the resolver (or the resume sweep) promotes them
    within seconds of first boot and they run to `completed`. The reset
    re-stamps their timestamps from `_dag_specs()`, whose `run_seconds` is
    `None` — so it writes NULL `started_at`/`completed_at` onto rows that are
    `completed`.

    Restoring the statuses is NOT the fix: the reset clears `chaos:*` first and
    the resume sweep ticks every 10 s, so a restored `WAITING` row races the
    sweep that is about to promote it again (WO-R3-274 gotcha). The honest
    repair is to say so, and to point a reader at the hook that manufactures a
    stranded chain on purpose — which is what this pins.
    """
    text = _reset_source()
    assert "drained on first boot" in text
    assert "create_stuck_dag" in text
    assert 'root_status="completed"' in text
    # And the reason the tempting repair is not taken, so nobody re-derives it.
    assert "races the resume sweep" in text


def test_the_seeded_trio_spec_still_declares_the_waiting_pair() -> None:
    """The counterpart of the paragraph above: it is only honest while the seed
    really does declare those two rows `waiting` with no dispatch times. If the
    seed ever changes, the paragraph is wrong and this fails."""
    seed = importlib.import_module("seed_eval_fixtures")

    by_name = {spec["name"]: spec for spec in seed._dag_specs()}
    assert by_name["dag-parent-job"]["status"] == JobStatus.COMPLETED.value
    for name in ("dag-seed-job", "dag-child-job"):
        assert by_name[name]["status"] == JobStatus.WAITING.value
        assert by_name[name]["run_seconds"] is None


# ---------------------------------------------------------------------------
# ADR 0029
# ---------------------------------------------------------------------------


def test_adr_0029_exists_and_is_indexed() -> None:
    adr = _REPO_ROOT / "docs" / "ADR" / (
        "0029-stranded-chain-and-lab-pause-are-manufactured.md"
    )
    assert adr.is_file(), "ADR 0029 is missing"
    index = (_REPO_ROOT / "docs" / "ADR" / "README.md").read_text(
        encoding="utf-8"
    )
    assert adr.name in index, "ADR 0029 is not in docs/ADR/README.md"
    assert adr.name in (_REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The deltas, enumerated — the rebless ledger cites this test by name
# ---------------------------------------------------------------------------


def test_the_shape_deltas_are_exactly_these(chaos_registered: None) -> None:
    """Where the ledger's field list is pinned (same convention as
    `test_poison_and_mislabel_tool_descriptions.py::test_the_shape_deltas_are_exactly_these`).
    A shape change the ledger does not mention is what makes a re-pin
    surprising."""
    stuck = get_tool("create_stuck_dag")
    assert stuck is not None
    # +root_status, +child_age_seconds, +failed_step on the way in.
    assert set(stuck.input_model.model_fields) == {
        "chain_name",
        "waiting_steps",
        "job_type",
        "remediation_hint",
        "error_message",
        "root_status",
        "child_age_seconds",
        "failed_step",
    }
    # +step_job_ids, +dead_letter_job_id on the way out: `waiting_job_ids` now
    # means what it says, so the two facts it used to conflate need their own
    # fields.
    assert set(stuck.output_model.model_fields) == {
        "root_job_id",
        "completed_parent_id",
        "waiting_job_ids",
        "step_job_ids",
        "dead_letter_job_id",
        "chain_name",
        "created",
        "accepted",
    }

    # Wholly new tool — a tool-level delta, not a field-level one.
    lab_pause = get_tool("pause_dag_chaos")
    assert lab_pause is not None
    assert set(lab_pause.input_model.model_fields) == {
        "root_job_id",
        "ttl_seconds",
    }
    assert set(lab_pause.output_model.model_fields) == {
        "root_job_id",
        "pause_key",
        "ttl_seconds",
        "accepted",
    }


def test_the_chaos_surface_grows_by_exactly_one_tool(
    whole_chaos_surface_registered: None,
) -> None:
    """32 → 33 with `CHAOS_ENABLED=true`, 12 of them chaos.

    Counted off the registry rather than trusted from a number in a doc, for
    the reason CLAUDE.md gives about this exact figure: it has drifted before.
    """
    names = {t.name for t in list_tools()}
    chaos_names = {
        t.name for t in list_tools() if t.required_scope == Scope.CHAOS_INVOKE
    }
    assert "pause_dag_chaos" in chaos_names
    assert len(chaos_names) == 12, sorted(chaos_names)
    assert len(names) == 33, sorted(names)


def test_no_new_refusal_code_reaches_the_commanders_chaos_client() -> None:
    """The ledger carries refusal codes because the commander's ChaosClient
    buckets an unknown `error_code` as a transport fault (R2-16). This packet
    adds none: the stranded chain reuses `stuck_chain_name_in_use`, the two
    input validators refuse as JSON-RPC invalid params, and the lab pause
    reuses `not_found` for a root that is missing or in another tenant —
    the same code, and the same message shape, `pause_dag` already returns.
    """
    from app.core.exceptions import NotFoundError
    from app.mcp.tools.chaos.create_stuck_dag import CreateStuckDagError

    assert CreateStuckDagError.error_code == "stuck_chain_name_in_use"
    assert CreateStuckDagError.status_code == 409
    assert NotFoundError.error_code == "not_found"
