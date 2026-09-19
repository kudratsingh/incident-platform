"""`saturate_db_pool` — the pool starves, the queries do not (WO-R3-219, WP-8.3).

The claim half of the hook: bounds, the key, which process's pool it holds, and the two
guarantees that keep it a fault rather than an outage — the free floor the clamp keeps, and the
three ways every connection comes back. The rebless deltas for the next re-pin are enumerated at
the bottom of this file, with `degrade_downstream`'s, because the two land in one PR.
"""

from __future__ import annotations

import ast
import asyncio
import fnmatch
import importlib
import pathlib
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

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
from app.workers import db_pool_hold
from app.workers.control_loop_pause import LOOP_FUNCTIONS, ControlLoopName
from pydantic import ValidationError

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_DISPATCHER = _REPO_ROOT / "backend" / "app" / "workers" / "dispatcher.py"

_CHAOS_MODULES = ("saturate_db_pool", "degrade_downstream")


@pytest.fixture
def chaos_registered() -> Iterator[None]:
    """Both hooks are chaos-gated, so reload them under patched settings and restore (ADR 0008
    gate 1)."""
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
    """Every chaos hook registered, for the one test that counts the surface."""
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


def _chaos_on() -> Any:
    return patch.object(
        db_pool_hold,
        "get_settings",
        return_value=Settings(chaos_enabled=True, environment="test"),
    )


class _FakeSession:
    def __init__(self, ledger: list[str]) -> None:
        self._ledger = ledger
        self.closed = False

    async def execute(self, *_a: Any, **_k: Any) -> None:
        self._ledger.append("execute")

    async def close(self) -> None:
        self.closed = True
        self._ledger.append("close")


class _FakeFactory:
    """A session factory with the one attribute `pool_capacity` reads, and a refusal dial."""

    def __init__(self, refuse_after: int | None = None) -> None:
        self.kw: dict[str, Any] = {}
        self.ledger: list[str] = []
        self.opened: list[_FakeSession] = []
        self._refuse_after = refuse_after

    def __call__(self) -> _FakeSession:
        if self._refuse_after is not None and len(self.opened) >= self._refuse_after:
            raise RuntimeError("pool exhausted")
        session = _FakeSession(self.ledger)
        self.opened.append(session)
        return session


class _Redis:
    def __init__(self, value: str | None = None, raises: bool = False) -> None:
        self.value = value
        self.raises = raises
        self.sets: list[tuple[str, Any, int | None]] = []

    async def get(self, _key: str) -> str | None:
        if self.raises:
            raise RuntimeError("redis is down")
        return self.value

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self.sets.append((key, value, ex))
        return True


# Gate 1, scope, blast radius


def test_the_hook_is_absent_from_the_registry_when_chaos_is_disabled() -> None:
    """Asserted with no fixture: the unit tier's default settings."""
    from app.mcp.tools.chaos import saturate_db_pool  # noqa: F401

    assert "saturate_db_pool" not in {t.name for t in list_tools()}


def test_the_hook_requires_chaos_invoke_and_declares_a_blast_radius(
    chaos_registered: None,
) -> None:
    """`shared_dependency`, the label `saturate_redis` already carries: one process's pool is
    shared by its API handlers and all eleven of its loops."""
    spec = get_tool("saturate_db_pool")
    assert spec is not None
    assert spec.required_scope == Scope.CHAOS_INVOKE
    assert spec.is_chaos is True
    assert spec.description.startswith(
        f"[chaos: {BlastRadius.SHARED_DEPENDENCY.value}] "
    )


def test_the_input_is_bounded_by_the_holders_own_ceiling(
    chaos_registered: None,
) -> None:
    """One ceiling, not two: a schema that promised more than the runtime clamp allows would
    report `accepted: true` for a hold nothing takes."""
    spec = get_tool("saturate_db_pool")
    assert spec is not None
    schema = spec.input_model.model_json_schema()
    assert schema["properties"]["connections"]["maximum"] == (
        db_pool_hold.MAX_HELD_CONNECTIONS
    )
    assert schema["properties"]["connections"]["minimum"] == 1
    assert schema["properties"]["ttl_seconds"]["maximum"] == 3600
    with pytest.raises(ValidationError):
        spec.input_model(connections=db_pool_hold.MAX_HELD_CONNECTIONS + 1)
    with pytest.raises(ValidationError):
        spec.input_model(ttl_seconds=0)
    with pytest.raises(ValidationError):
        spec.input_model(unknown_dial=1)


def test_the_defaults_are_a_usable_fault(chaos_registered: None) -> None:
    """A chaos hook called with no arguments has to produce the world it exists for."""
    spec = get_tool("saturate_db_pool")
    assert spec is not None
    inp = spec.input_model()
    assert inp.connections == db_pool_hold.MAX_HELD_CONNECTIONS
    assert inp.ttl_seconds == 300


def test_neither_model_carries_a_class_docstring(chaos_registered: None) -> None:
    """A class docstring serializes as the schema `description`, and both schemas reach
    `tools/list` — a contract change disguised as a comment."""
    for name in ("saturate_db_pool", "degrade_downstream"):
        spec = get_tool(name)
        assert spec is not None
        for model in (spec.input_model, spec.output_model):
            assert "description" not in model.model_json_schema(), (
                f"{name}: {model.__name__} has a class docstring"
            )


# The key, and its three teardowns


async def test_the_hook_writes_the_count_with_the_ttl(
    chaos_registered: None,
) -> None:
    from app.dependencies import Principal
    from app.mcp.registry import ToolContext

    spec = get_tool("saturate_db_pool")
    assert spec is not None
    redis = _Redis()
    ctx = ToolContext(
        db=None,  # type: ignore[arg-type]
        redis=redis,
        principal=Principal(
            kind="service_account",
            tenant_id=__import__("uuid").uuid4(),
            scopes=frozenset({Scope.CHAOS_INVOKE.value}),
        ),
    )
    out = await spec.handler(spec.input_model(connections=7, ttl_seconds=60), ctx)

    assert redis.sets == [(db_pool_hold.HOLD_KEY, "7", 60)]
    assert out.hold_key == db_pool_hold.HOLD_KEY
    assert out.connections == 7
    assert out.min_free_connections == db_pool_hold.MIN_FREE_CONNECTIONS
    assert out.poll_interval_seconds == db_pool_hold.POLL_INTERVAL_SECONDS
    assert out.accepted is True


def test_the_hold_key_lives_under_the_chaos_namespace() -> None:
    assert fnmatch.fnmatch(db_pool_hold.hold_key(), "chaos:*")


def test_make_eval_reset_sweeps_the_hold_key() -> None:
    """The reset's own pattern tuple is the authority (04:129). This is the teardown the test
    requirement asks for: the reset releases the connections even with TTL left, because the
    holder gives them back on the first pass after the key is gone."""
    from tests.unit.test_eval_reset import _reset_module

    patterns = _reset_module()._CHAOS_KEY_PATTERNS
    key = db_pool_hold.hold_key()
    assert any(fnmatch.fnmatch(key, p) for p in patterns), (
        f"{key} matches no pattern in {patterns}"
    )


# What the description has to say, because the description is the whole interface


def test_the_description_says_which_process_pool_is_held(
    chaos_registered: None,
) -> None:
    """The correctness question this packet had to settle: a reading taken in the MCP process is
    a reading of a different pool (CLAUDE.md:205)."""
    spec = get_tool("saturate_db_pool")
    assert spec is not None
    text = spec.description
    assert "the API and worker process's pool" in text
    assert "MCP server is a separate process with its own pool" in text


def test_the_description_says_the_queries_stay_normal(
    chaos_registered: None,
) -> None:
    """The discrimination the family turns on: A2 is waiting to acquire, A1 is a slow query."""
    spec = get_tool("saturate_db_pool")
    assert spec is not None
    assert "wait to ACQUIRE a connection" in spec.description
    assert "runs at its normal speed" in spec.description


def test_the_description_says_the_loops_keep_running(
    chaos_registered: None,
) -> None:
    spec = get_tool("saturate_db_pool")
    assert spec is not None
    assert "stay acquirable" in spec.description
    assert "slow down rather than stop" in spec.description


def test_the_description_says_a_repeat_call_replaces_the_hold(
    chaos_registered: None,
) -> None:
    """One key, so two calls are one hold — idempotent in fixture identity (01 §11)."""
    spec = get_tool("saturate_db_pool")
    assert spec is not None
    assert "replaces the hold instead of adding a second one" in spec.description


def test_the_output_says_acceptance_is_not_yet_a_hold(
    chaos_registered: None,
) -> None:
    spec = get_tool("saturate_db_pool")
    assert spec is not None
    described = spec.output_model.model_json_schema()["properties"]["accepted"]
    assert "does not confirm the connections are held" in described["description"]


# Reading the flag


async def test_the_flag_is_not_read_at_all_when_chaos_is_disabled() -> None:
    """Gate 1 on the hot path: production must not pay a Redis round-trip per pass for a lab
    feature."""
    redis = AsyncMock()
    with patch.object(
        db_pool_hold,
        "get_settings",
        return_value=Settings(chaos_enabled=False, environment="test"),
    ):
        assert await db_pool_hold.requested_hold(redis) == 0
    redis.get.assert_not_awaited()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 0),
        ("0", 0),
        ("3", 3),
        (b"4", 4),
        ("not-a-count", 0),
        ("-2", 0),
        ("99", db_pool_hold.MAX_HELD_CONNECTIONS),
    ],
)
async def test_the_flag_is_read_as_a_bounded_count(raw: Any, expected: int) -> None:
    with _chaos_on():
        assert await db_pool_hold.requested_hold(_Redis(value=raw)) == expected


async def test_the_flag_read_fails_open() -> None:
    """An unreadable flag releases the hold rather than keeping it: a Redis blip is not a fault
    to inject, and the same rule as every other flag read in this repo."""
    with _chaos_on():
        assert await db_pool_hold.requested_hold(_Redis(raises=True)) == 0


# The clamp — the guarantee that this is a fault and not an outage


@pytest.mark.parametrize(
    ("wanted", "capacity", "expected"),
    [
        (10, 15, 10),  # stock pool: 5 + 10 overflow, four left free by capacity
        (10, 12, 8),  # a smaller pool clamps harder
        (10, 4, 0),  # a pool that cannot spare one is not saturated at all
        (10, None, db_pool_hold.MAX_HELD_CONNECTIONS),  # SQLite: static cap only
        (99, 15, db_pool_hold.MAX_HELD_CONNECTIONS),
        (0, 15, 0),
    ],
)
def test_the_clamp_always_leaves_the_free_floor(
    wanted: int, capacity: int | None, expected: int
) -> None:
    held = db_pool_hold.clamp_to_pool(wanted, capacity)
    assert held == expected
    if capacity is not None:
        assert capacity - held >= db_pool_hold.MIN_FREE_CONNECTIONS or held == 0


def test_the_free_floor_covers_the_loop_every_job_depends_on() -> None:
    """Stated as a number so a later change to it is a decision: the outbox relay, the
    dispatcher's claim, and two spare."""
    assert db_pool_hold.MIN_FREE_CONNECTIONS == 4


def test_the_capacity_reader_is_quiet_about_a_pool_it_cannot_read() -> None:
    assert db_pool_hold.pool_capacity(_FakeFactory()) is None  # type: ignore[arg-type]


# Holding, and giving back


async def test_reconcile_takes_and_gives_back_connections() -> None:
    factory = _FakeFactory()
    held: list[Any] = []

    assert await db_pool_hold.reconcile_held(held, 3, factory) == 3  # type: ignore[arg-type]
    assert len(factory.opened) == 3
    assert factory.ledger.count("execute") == 3

    assert await db_pool_hold.reconcile_held(held, 1, factory) == 1  # type: ignore[arg-type]
    assert sum(1 for s in factory.opened if s.closed) == 2

    assert await db_pool_hold.reconcile_held(held, 0, factory) == 0  # type: ignore[arg-type]
    assert all(s.closed for s in factory.opened)


async def test_reconcile_stops_at_a_pool_that_refuses() -> None:
    """The floor is a promise, not a hope: if the pool will not hand over another connection the
    holder keeps what it has instead of spinning."""
    factory = _FakeFactory(refuse_after=2)
    held: list[Any] = []
    assert await db_pool_hold.reconcile_held(held, 5, factory) == 2  # type: ignore[arg-type]


async def test_the_holder_returns_at_once_when_chaos_is_disabled() -> None:
    factory = _FakeFactory()
    redis = AsyncMock()
    with patch.object(
        db_pool_hold,
        "get_settings",
        return_value=Settings(chaos_enabled=False, environment="test"),
    ):
        await asyncio.wait_for(
            db_pool_hold.hold_db_pool(factory, redis),  # type: ignore[arg-type]
            timeout=1,
        )
    assert factory.opened == []
    redis.get.assert_not_awaited()


async def test_the_holder_releases_everything_when_it_is_cancelled() -> None:
    """A worker restart is the third teardown, and `finally` is what makes it one."""
    factory = _FakeFactory()
    redis = _Redis(value="2")
    with _chaos_on(), patch.object(db_pool_hold, "POLL_INTERVAL_SECONDS", 0.01):
        task = asyncio.create_task(
            db_pool_hold.hold_db_pool(factory, redis)  # type: ignore[arg-type]
        )
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(factory.opened) >= 2:
                break
        assert len(factory.opened) == 2
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert all(s.closed for s in factory.opened)


# Not a twelfth loop (ADR 0031)


def _worker_loop_node() -> ast.AsyncFunctionDef:
    tree = ast.parse(_DISPATCHER.read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "worker_loop"
    )


def test_the_holder_is_started_only_under_the_chaos_gate() -> None:
    """ADR 0008 gate 1 for a task rather than a tool: nothing of this exists in production."""
    worker_loop = _worker_loop_node()
    rendered = ast.unparse(worker_loop)
    assert rendered.count("hold_db_pool") == 1, (
        "the pool holder is started in more than one place, so one of them is ungated"
    )
    gated = [
        node
        for node in ast.walk(worker_loop)
        if isinstance(node, ast.If)
        and "chaos_enabled" in ast.unparse(node.test)
        and "hold_db_pool" in ast.unparse(node)
    ]
    assert gated, "the pool holder is not behind an `if ...chaos_enabled` gate"


def test_the_holder_is_not_one_of_the_pausable_loops() -> None:
    """ADR 0027's enum stays closed at eleven: this task's off switch is its own key, and
    `pause_control_loop` must not grow a member for a lab task (ADR 0031)."""
    assert len(ControlLoopName) == 11
    assert "hold_db_pool" not in set(LOOP_FUNCTIONS.values())
    assert not any("db_pool" in member.value for member in ControlLoopName)


# ADR 0031


def test_adr_0031_exists_and_is_indexed() -> None:
    adr = (
        _REPO_ROOT
        / "docs"
        / "ADR"
        / "0031-a-held-pool-and-a-degraded-dependency-are-flagged-not-broken.md"
    )
    assert adr.is_file(), "ADR 0031 is missing"
    index = (_REPO_ROOT / "docs" / "ADR" / "README.md").read_text(encoding="utf-8")
    assert adr.name in index, "ADR 0031 is not in docs/ADR/README.md"
    assert adr.name in (_REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")


# The deltas, enumerated — the rebless ledger cites this test by name


def test_the_shape_deltas_are_exactly_these(chaos_registered: None) -> None:
    """Where the ledger's field list is pinned, for both hooks in this PR: a shape change it does
    not mention is what makes a re-pin surprising."""
    pool = get_tool("saturate_db_pool")
    assert pool is not None
    assert set(pool.input_model.model_fields) == {"connections", "ttl_seconds"}
    assert set(pool.output_model.model_fields) == {
        "hold_key",
        "connections",
        "ttl_seconds",
        "poll_interval_seconds",
        "min_free_connections",
        "accepted",
    }

    downstream = get_tool("degrade_downstream")
    assert downstream is not None
    assert set(downstream.input_model.model_fields) == {
        "mode",
        "delay_ms",
        "ttl_seconds",
    }
    assert set(downstream.output_model.model_fields) == {
        "dependency",
        "flag_key",
        "mode",
        "delay_ms",
        "ttl_seconds",
        "failure_threshold",
        "recovery_timeout_seconds",
        "accepted",
    }


def test_the_chaos_surface_grows_by_exactly_two_tools(
    whole_chaos_surface_registered: None,
) -> None:
    """33 → 35 with `CHAOS_ENABLED=true`, 14 of them chaos, counted off the registry — CLAUDE.md
    says this figure has drifted before. The total moved to 37 with WO-R3-217's two read tools; the
    chaos half, which is what this test is about, did not move."""
    names = {t.name for t in list_tools()}
    chaos_names = {
        t.name for t in list_tools() if t.required_scope == Scope.CHAOS_INVOKE
    }
    assert {"saturate_db_pool", "degrade_downstream"} <= chaos_names
    assert len(chaos_names) == 14, sorted(chaos_names)
    assert len(names) == 37, sorted(names)


def test_neither_hook_adds_a_refusal_code_to_the_commanders_chaos_client() -> None:
    """The ChaosClient buckets an unknown `error_code` as a transport fault (R2-16). Both hooks
    refuse only as JSON-RPC invalid params, so there is no new code to ledger."""
    import app.mcp.tools.chaos.degrade_downstream as downstream_mod
    import app.mcp.tools.chaos.saturate_db_pool as pool_mod

    for module in (pool_mod, downstream_mod):
        assert not [
            name
            for name, obj in vars(module).items()
            if isinstance(obj, type) and issubclass(obj, Exception)
        ], f"{module.__name__} defines an exception; the ledger would need its code"
