"""`slow_db_queries` — the queries run long, the pool does not fill (WO-R3-218, WP-8.2).

The claim half of the hook plus the arithmetic that makes it observable at all: the chunk floor
that keeps `longest_active_query_ms` above the platform's slow threshold at every instant, the
closed target map that keeps caller text out of the statement, and the three teardowns. The
rebless deltas for the next re-pin are enumerated at the bottom of this file.
"""

from __future__ import annotations

import ast
import asyncio
import fnmatch
import importlib
import inspect
import pathlib
import time
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
from app.mcp.tools.health import SLOW_QUERY_THRESHOLD_MS
from app.workers import db_slow_query
from app.workers.control_loop_pause import LOOP_FUNCTIONS, ControlLoopName
from app.workers.db_pool_hold import MIN_FREE_CONNECTIONS
from pydantic import ValidationError
from tests.conftest import AbortingSession
from tests.unit.test_lab_invisibility import LAB_VOCABULARY

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_DISPATCHER = _REPO_ROOT / "backend" / "app" / "workers" / "dispatcher.py"


@pytest.fixture
def chaos_registered() -> Iterator[None]:
    """The hook is chaos-gated, so reload it under patched settings and restore (ADR 0008 gate
    1)."""
    from app.mcp import chaos as chaos_mod
    from app.mcp.tools import chaos as chaos_pkg

    module = chaos_pkg.slow_query
    snapshot = _snapshot_for_tests()
    try:
        with patch.object(
            chaos_mod,
            "get_settings",
            return_value=Settings(chaos_enabled=True, environment="test"),
        ):
            importlib.reload(module)
        yield
    finally:
        _restore_for_tests(snapshot)
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
        db_slow_query,
        "get_settings",
        return_value=Settings(chaos_enabled=True, environment="test"),
    )


class _FakeSession:
    """Records the statement it was given and whether it was closed."""

    def __init__(self, ledger: list[str], *, fail: bool = False) -> None:
        self._ledger = ledger
        self._fail = fail
        self.closed = False
        self.statements: list[str] = []

    async def execute(self, statement: Any, params: Any = None) -> None:
        self.statements.append(str(statement))
        self._ledger.append("execute")
        if self._fail:
            raise RuntimeError("no")
        seconds = (params or {}).get("seconds", 0)
        await asyncio.sleep(seconds)

    async def close(self) -> None:
        self.closed = True
        self._ledger.append("close")


class _FakeFactory:
    """A session factory with the one attribute `pool_capacity` reads."""

    def __init__(self, *, capacity: int | None = 15, fail: bool = False) -> None:
        self.kw: dict[str, Any] = {}
        self.ledger: list[str] = []
        self.opened: list[_FakeSession] = []
        self._fail = fail
        if capacity is not None:
            self.kw = {"bind": _FakeBind(capacity)}

    def __call__(self) -> _FakeSession:
        session = _FakeSession(self.ledger, fail=self._fail)
        self.opened.append(session)
        return session

    @property
    def in_flight(self) -> int:
        return len([s for s in self.opened if not s.closed])


class _FakePool:
    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._max_overflow = 0

    def size(self) -> int:
        return self._capacity


class _FakeBind:
    def __init__(self, capacity: int) -> None:
        self.pool = _FakePool(capacity)


class _Redis:
    """One key, with an expiry this test can drive by the clock."""

    def __init__(self, value: str | None = None) -> None:
        self._value = value
        self._expires_at: float | None = None

    async def get(self, _key: str) -> str | None:
        if self._expires_at is not None and time.monotonic() >= self._expires_at:
            self._value = None
            self._expires_at = None
        return self._value

    async def set(self, _key: str, value: Any, ex: int | None = None) -> bool:
        self._value = str(value)
        self._expires_at = time.monotonic() + ex if ex is not None else None
        return True

    async def delete(self, *_keys: str) -> int:
        removed = 1 if self._value is not None else 0
        self._value = None
        self._expires_at = None
        return removed


# The gate


def test_the_hook_is_absent_when_chaos_is_disabled() -> None:
    """ADR 0008 gate 1: the agent cannot see that the lab exists."""
    assert get_tool("slow_db_queries") is None


def test_the_hook_registers_under_the_gate(chaos_registered: None) -> None:
    tool = get_tool("slow_db_queries")
    assert tool is not None
    assert tool.required_scope == Scope.CHAOS_INVOKE
    assert tool.is_chaos
    assert tool.description.startswith(
        f"[chaos: {BlastRadius.SHARED_DEPENDENCY.value}]"
    )


def test_the_blast_radius_is_the_one_saturate_db_pool_carries(
    chaos_registered: None,
) -> None:
    """One process's pool and one database are shared by its API handlers and all eleven of its
    loops, so this is `shared_dependency` for the same reason its sibling is — and it adds no
    sixth member to that enum."""
    tool = get_tool("slow_db_queries")
    assert tool is not None
    assert BlastRadius.SHARED_DEPENDENCY.value in tool.description
    assert len(BlastRadius) == 5


# The declared scope


def test_every_declared_target_names_a_relation(chaos_registered: None) -> None:
    from app.mcp.tools.chaos.slow_query import SlowQueryTarget

    assert {member.value for member in SlowQueryTarget} == set(
        db_slow_query.TARGET_RELATIONS
    )


@pytest.mark.parametrize(
    "target",
    ["users", "pg_stat_activity", "job_reads ", "JOB_READS", "jobs", ""],
)
def test_an_undeclared_target_is_refused(chaos_registered: None, target: str) -> None:
    """"Refused rather than matched against nothing": the scope is a closed enum, so an
    undeclared target never reaches the task at all."""
    tool = get_tool("slow_db_queries")
    assert tool is not None
    with pytest.raises(ValidationError):
        tool.input_model.model_validate({"target": target})


@pytest.mark.parametrize(
    "field,value",
    [
        ("query_ms", db_slow_query.MIN_QUERY_MS - 1),
        ("query_ms", db_slow_query.MAX_QUERY_MS + 1),
        ("ttl_seconds", 0),
        ("ttl_seconds", 3601),
    ],
)
def test_the_bounds_are_enforced_on_input(
    chaos_registered: None, field: str, value: int
) -> None:
    tool = get_tool("slow_db_queries")
    assert tool is not None
    with pytest.raises(ValidationError):
        tool.input_model.model_validate({field: value})


def test_no_extra_input_is_accepted(chaos_registered: None) -> None:
    tool = get_tool("slow_db_queries")
    assert tool is not None
    with pytest.raises(ValidationError):
        tool.input_model.model_validate({"concurrent_queries": 8})


# Observable by construction


def test_the_chunk_floor_keeps_the_reading_continuous() -> None:
    """The whole fixture rests on this inequality. With `SLEEPER_COUNT` queries evenly offset
    inside a chunk, the oldest one in flight is never younger than a chunk's (n-1)/n — so if that
    is above the platform's slow threshold, `active_queries_over_slow_threshold` never dips to 0
    between queries. A floor below it would ship a fault the agent sees only some of the time."""
    n = db_slow_query.SLEEPER_COUNT
    assert n >= 2, "one sleeper makes the reading a sawtooth through the threshold"
    oldest_in_flight_ms = db_slow_query.MIN_QUERY_MS * (n - 1) / n
    assert oldest_in_flight_ms > SLOW_QUERY_THRESHOLD_MS, (
        f"a {db_slow_query.MIN_QUERY_MS} ms chunk across {n} sleepers leaves the oldest query "
        f"at {oldest_in_flight_ms} ms, at or below the {SLOW_QUERY_THRESHOLD_MS} ms threshold"
    )
    assert db_slow_query.MIN_QUERY_MS <= db_slow_query.DEFAULT_QUERY_MS
    assert db_slow_query.DEFAULT_QUERY_MS <= db_slow_query.MAX_QUERY_MS


def test_the_statement_really_reads_the_declared_relation() -> None:
    """A sleep on its own would make `target` decoration. The statement reads the relation, so a
    reader of `pg_stat_activity` sees which read path is slow."""
    for target, relation in db_slow_query.TARGET_RELATIONS.items():
        rendered = str(db_slow_query.statement_for(target))
        assert "pg_sleep" in rendered
        assert f"count(*) FROM {relation}" in rendered
    assert len(set(db_slow_query.TARGET_RELATIONS.values())) == len(
        db_slow_query.TARGET_RELATIONS
    ), "two targets naming one relation would be two names for one fault"


def test_the_statements_carry_no_lab_vocabulary() -> None:
    """No read tool returns query text today — `get_postgres_health` says so — but the statement
    is the one string the fault puts on the database server, so it is written as though one
    did (ADR 0012 rule 1)."""
    for target in db_slow_query.TARGET_RELATIONS:
        rendered = str(db_slow_query.statement_for(target))
        assert not LAB_VOCABULARY.search(rendered), rendered


def test_the_lab_stays_invisible_with_the_hook_armed(
    whole_chaos_surface_registered: None,
) -> None:
    """The chaos-vocabulary screen, re-run with every hook registered rather than with the gate
    closed: this hook adds a `chaos:` key and a worker task, and nothing a non-chaos principal
    can pull off `tools/list` may echo either (ADR 0012 rule 1)."""
    from tests.unit.test_lab_invisibility import _wire_surface

    offenders = {
        td.name: sorted({m.group(0) for m in LAB_VOCABULARY.finditer(_wire_surface(td))})
        for td in list_tools()
        if td.required_scope != Scope.CHAOS_INVOKE
        and LAB_VOCABULARY.search(_wire_surface(td))
    }
    assert offenders == {}, offenders
    assert "slow_db_queries" in {
        td.name for td in list_tools() if td.required_scope == Scope.CHAOS_INVOKE
    }, "the screen ran with the hook absent, so it proved nothing about it"


def test_the_relation_never_comes_from_the_flag() -> None:
    """The value is data, the relation is a lookup. A flag written by hand cannot reach the
    statement text."""
    assert db_slow_query.parse_request("jobs; DROP TABLE jobs:2000") is None
    assert db_slow_query.parse_request("job_reads:2000") == db_slow_query.SlowQueryRequest(
        target="job_reads", query_ms=2000
    )
    source = pathlib.Path(db_slow_query.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    statement_for = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "statement_for"
    )
    assert "TARGET_RELATIONS[target]" in ast.unparse(statement_for)


# The flag


async def test_the_flag_it_writes_is_the_one_the_task_reads(
    chaos_registered: None,
) -> None:
    from app.mcp.tools.chaos import slow_query as hook

    ctx = _ctx()
    out = await hook.slow_db_queries(
        hook.SlowQueryInput(target=hook.SlowQueryTarget.AUDIT_READS, query_ms=1500),
        ctx,
    )
    assert out.flag_key == db_slow_query.SLOW_QUERY_KEY
    assert out.relation == "audit_logs"
    assert out.concurrent_queries == db_slow_query.SLEEPER_COUNT
    assert out.slow_query_threshold_ms == SLOW_QUERY_THRESHOLD_MS
    assert out.accepted is True
    ctx.redis.set.assert_awaited_once_with(
        db_slow_query.SLOW_QUERY_KEY, "audit_reads:1500", ex=300
    )
    assert db_slow_query.parse_request(
        "audit_reads:1500"
    ) == db_slow_query.SlowQueryRequest(target="audit_reads", query_ms=1500)


async def test_a_repeat_call_replaces_the_fault(chaos_registered: None) -> None:
    """One key, so a second call is the new state rather than a second fault on top."""
    from app.mcp.tools.chaos import slow_query as hook

    redis = _Redis()
    ctx = _ctx(redis=redis)
    await hook.slow_db_queries(hook.SlowQueryInput(query_ms=1200), ctx)
    await hook.slow_db_queries(
        hook.SlowQueryInput(target=hook.SlowQueryTarget.OUTBOX_READS, query_ms=9000),
        ctx,
    )
    assert await redis.get(db_slow_query.SLOW_QUERY_KEY) == "outbox_reads:9000"


def test_the_flag_lives_under_the_chaos_namespace() -> None:
    """The reset's single `chaos:*` scan is only complete while this holds, so assert it here as
    well as in `test_eval_reset.py`."""
    assert fnmatch.fnmatch(db_slow_query.slow_query_key(), "chaos:*")


@pytest.mark.parametrize(
    "value",
    ["users:2000", "job_reads:nine", "job_reads", "job_reads:900", "job_reads:20000", ""],
)
def test_a_flag_the_hook_did_not_write_reads_as_off(value: str) -> None:
    """Fail closed on the target, fail open on the fault: anything unrecognised means no slow
    query rather than a guess at one."""
    assert db_slow_query.parse_request(value) is None


async def test_an_unreadable_flag_ends_the_fault() -> None:
    redis = AsyncMock()
    redis.get.side_effect = RuntimeError("redis is gone")
    with _chaos_on():
        assert await db_slow_query.requested_slow_query(redis) is None


async def test_the_flag_is_not_read_at_all_when_chaos_is_disabled() -> None:
    redis = AsyncMock()
    with patch.object(
        db_slow_query,
        "get_settings",
        return_value=Settings(chaos_enabled=False, environment="test"),
    ):
        assert await db_slow_query.requested_slow_query(redis) is None
    redis.get.assert_not_awaited()


# The pool floor


@pytest.mark.parametrize(
    "capacity,budget",
    [
        (None, db_slow_query.SLEEPER_COUNT),
        (15, db_slow_query.SLEEPER_COUNT),
        (MIN_FREE_CONNECTIONS + db_slow_query.SLEEPER_COUNT, db_slow_query.SLEEPER_COUNT),
        (MIN_FREE_CONNECTIONS + 1, 0),
        (MIN_FREE_CONNECTIONS, 0),
        (2, 0),
    ],
)
def test_the_sleepers_never_take_the_last_connections(
    capacity: int | None, budget: int
) -> None:
    """The same floor `saturate_db_pool` keeps, for the same reason: the loops have to keep
    running. A pool that cannot spare the sleepers runs none of them."""
    assert db_slow_query.sleeper_budget(capacity) == budget


async def test_a_pool_that_cannot_spare_a_connection_runs_no_query() -> None:
    factory = _FakeFactory(capacity=MIN_FREE_CONNECTIONS + 1)
    redis = _Redis(value="job_reads:1200")
    with _chaos_on():
        await asyncio.wait_for(
            db_slow_query.run_slow_queries(factory, redis),  # type: ignore[arg-type]
            timeout=1,
        )
    assert factory.opened == []


async def test_the_task_returns_at_once_when_chaos_is_disabled() -> None:
    factory = _FakeFactory()
    redis = AsyncMock()
    with patch.object(
        db_slow_query,
        "get_settings",
        return_value=Settings(chaos_enabled=False, environment="test"),
    ):
        await asyncio.wait_for(
            db_slow_query.run_slow_queries(factory, redis),  # type: ignore[arg-type]
            timeout=1,
        )
    assert factory.opened == []
    redis.get.assert_not_awaited()


# Running, stopping, and giving the connections back


async def _run_until(task: asyncio.Task[Any], predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.01)
        if predicate():
            return True
        if task.done():
            break
    return False


async def test_both_sleepers_run_and_are_offset_inside_the_chunk() -> None:
    """Two queries in flight at once is what keeps the reading continuous; the offset is what
    keeps them from starting and finishing together."""
    factory = _FakeFactory()
    redis = _Redis(value=f"job_reads:{db_slow_query.MIN_QUERY_MS}")
    with _chaos_on():
        task = asyncio.create_task(
            db_slow_query.run_slow_queries(factory, redis)  # type: ignore[arg-type]
        )
        assert await _run_until(task, lambda: factory.in_flight >= 2)
        starts = len(factory.opened)
        # The offset means the two never turn over on the same tick: after one chunk the
        # earlier sleeper has started its next query while the later one is still on its first.
        assert await _run_until(task, lambda: len(factory.opened) > starts)
        assert factory.in_flight == 2
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert all(s.closed for s in factory.opened)


async def test_clearing_the_flag_stops_the_queries_with_nothing_to_call() -> None:
    """What `make eval-reset`'s `chaos:*` sweep does, from the task's side."""
    factory = _FakeFactory()
    redis = _Redis(value=f"job_reads:{db_slow_query.MIN_QUERY_MS}")
    with _chaos_on(), patch.object(db_slow_query, "POLL_INTERVAL_SECONDS", 0.01):
        task = asyncio.create_task(
            db_slow_query.run_slow_queries(factory, redis)  # type: ignore[arg-type]
        )
        assert await _run_until(task, lambda: factory.in_flight >= 2)
        await redis.delete(db_slow_query.SLOW_QUERY_KEY)
        assert await _run_until(task, lambda: factory.in_flight == 0)
        opened = len(factory.opened)
        await asyncio.sleep(0.2)
        assert len(factory.opened) == opened, "a cleared flag still started a query"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_expiry_restores_normal_timing_with_no_manual_step() -> None:
    """The TTL is the first of the three teardowns, and the one a scenario leans on."""
    factory = _FakeFactory()
    redis = _Redis()
    await redis.set(
        db_slow_query.SLOW_QUERY_KEY, f"job_reads:{db_slow_query.MIN_QUERY_MS}", ex=1
    )
    with _chaos_on(), patch.object(db_slow_query, "POLL_INTERVAL_SECONDS", 0.01):
        task = asyncio.create_task(
            db_slow_query.run_slow_queries(factory, redis)  # type: ignore[arg-type]
        )
        assert await _run_until(task, lambda: factory.in_flight >= 2)
        assert await _run_until(task, lambda: factory.in_flight == 0, timeout=6)
        opened = len(factory.opened)
        await asyncio.sleep(0.2)
        assert len(factory.opened) == opened
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert all(s.closed for s in factory.opened)


async def test_a_restart_gives_every_connection_back() -> None:
    """The third teardown, and `finally` is what makes it one."""
    factory = _FakeFactory()
    redis = _Redis(value=f"job_reads:{db_slow_query.MAX_QUERY_MS}")
    with _chaos_on():
        task = asyncio.create_task(
            db_slow_query.run_slow_queries(factory, redis)  # type: ignore[arg-type]
        )
        assert await _run_until(task, lambda: factory.in_flight >= 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert factory.opened
    assert all(s.closed for s in factory.opened)


# R2-59: a swallowed DB error must not reach a caller's transaction


async def test_a_failed_query_discards_its_session_instead_of_reusing_it() -> None:
    """The `AbortingSession` case (`CLAUDE.md` on `db_degrade`, WO-R2-59). This task swallows DB
    errors on purpose — a failed sleep must not stop the fault from being retried — so the
    session it swallowed one on must never be used again. It is not borrowed and not reused: a
    new one is opened for the next chunk, and the aborted one is closed. SQLite would not show
    this; `AbortingSession` aborts the way Postgres does."""
    ledger: list[str] = []
    opened: list[_Aborter] = []

    def factory() -> Any:
        session = _Aborter(_FakeSession(ledger), fail_on="pg_sleep")
        opened.append(session)
        return session

    factory.kw = {"bind": _FakeBind(15)}  # type: ignore[attr-defined]
    request = db_slow_query.SlowQueryRequest(target="job_reads", query_ms=1200)

    assert await db_slow_query.run_one_slow_query(factory, request) is False  # type: ignore[arg-type]
    assert await db_slow_query.run_one_slow_query(factory, request) is False  # type: ignore[arg-type]

    assert len(opened) == 2, "the second chunk reused the aborted session"
    assert all(s.aborted for s in opened)
    assert all(s.closed for s in opened)
    assert ledger.count("execute") == 0, (
        "the statement reached the inner session, so the abort was not the wrapper's"
    )


def test_the_task_opens_its_own_session_and_never_takes_one() -> None:
    """Structural, because this is the property that makes the test above hold for every future
    chunk: the sleeper is handed a factory, never a session."""
    params = inspect.signature(db_slow_query.run_one_slow_query).parameters
    assert list(params) == ["session_factory", "request"]
    source = pathlib.Path(db_slow_query.__file__).read_text(encoding="utf-8")
    assert "session_factory()" in source
    assert "await session.close()" in source


class _Aborter(AbortingSession):
    """`AbortingSession` plus the one thing this file asserts about it — that it was closed."""

    def __init__(self, inner: Any, *, fail_on: str) -> None:
        super().__init__(inner, fail_on=fail_on)
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        await self._inner.close()


# Not a twelfth loop (ADR 0027, and ADR 0031's reading of it)


def _worker_loop_node() -> ast.AsyncFunctionDef:
    tree = ast.parse(_DISPATCHER.read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "worker_loop"
    )


def test_the_task_is_started_only_under_the_chaos_gate() -> None:
    """ADR 0008 gate 1 for a task rather than a tool: nothing of this exists in production."""
    worker_loop = _worker_loop_node()
    rendered = ast.unparse(worker_loop)
    assert rendered.count("run_slow_queries") == 1, (
        "the slow-query task is started in more than one place, so one of them is ungated"
    )
    gated = [
        node
        for node in ast.walk(worker_loop)
        if isinstance(node, ast.If)
        and "chaos_enabled" in ast.unparse(node.test)
        and "run_slow_queries" in ast.unparse(node)
    ]
    assert gated, "the slow-query task is not behind an `if ...chaos_enabled` gate"


def test_the_task_is_not_one_of_the_pausable_loops() -> None:
    """ADR 0027's enum stays closed at eleven: this task's off switch is its own key, and
    `pause_control_loop` must not grow a member for a lab task."""
    assert len(ControlLoopName) == 11
    assert "run_slow_queries" not in set(LOOP_FUNCTIONS.values())
    assert not any("query" in member.value for member in ControlLoopName)


# ADR 0034


def test_adr_0034_exists_and_is_indexed() -> None:
    adr = (
        _REPO_ROOT
        / "docs"
        / "ADR"
        / "0034-a-slow-query-is-manufactured-where-the-server-can-see-it.md"
    )
    assert adr.is_file(), "ADR 0034 is missing"
    index = (_REPO_ROOT / "docs" / "ADR" / "README.md").read_text(encoding="utf-8")
    assert adr.name in index, "ADR 0034 is not in docs/ADR/README.md"
    assert adr.name in (_REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")


# The deltas, enumerated — the rebless ledger cites this test by name


def test_the_shape_deltas_are_exactly_these(chaos_registered: None) -> None:
    """Where the ledger's field list is pinned: a shape change it does not mention is what makes
    a re-pin surprising."""
    tool = get_tool("slow_db_queries")
    assert tool is not None
    assert set(tool.input_model.model_fields) == {
        "target",
        "query_ms",
        "ttl_seconds",
    }
    assert set(tool.output_model.model_fields) == {
        "flag_key",
        "target",
        "relation",
        "query_ms",
        "ttl_seconds",
        "concurrent_queries",
        "poll_interval_seconds",
        "slow_query_threshold_ms",
        "accepted",
    }
    assert tool.input_json_schema()["properties"]["target"]["default"] == "job_reads"


def test_the_chaos_surface_grows_by_exactly_one_tool(
    whole_chaos_surface_registered: None,
) -> None:
    """37 → 38 with `CHAOS_ENABLED=true`, 15 of them chaos, counted off the registry — CLAUDE.md
    says this figure has drifted before. The read tier does not move."""
    names = {t.name for t in list_tools()}
    chaos_names = {
        t.name for t in list_tools() if t.required_scope == Scope.CHAOS_INVOKE
    }
    assert "slow_db_queries" in chaos_names
    assert len(chaos_names) == 15, sorted(chaos_names)
    assert len(names) == 38, sorted(names)


def test_no_new_refusal_code_reaches_the_commanders_chaos_client() -> None:
    """The ChaosClient buckets an unknown `error_code` as a transport fault (R2-16). This hook
    refuses only as JSON-RPC invalid params, so there is no new code to ledger."""
    import app.mcp.tools.chaos.slow_query as module

    assert not [
        name
        for name, obj in vars(module).items()
        if isinstance(obj, type) and issubclass(obj, Exception)
    ], "the hook defines an exception; the ledger would need its code"


def _ctx(redis: Any = None) -> Any:
    from uuid import uuid4

    class _Principal:
        tenant_id = uuid4()

    class _Ctx:
        def __init__(self, r: Any) -> None:
            self.redis = r if r is not None else AsyncMock()
            self.principal = _Principal()

    return _Ctx(redis)
