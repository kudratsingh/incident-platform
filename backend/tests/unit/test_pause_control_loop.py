"""`pause_control_loop` — the enum is only a safety boundary if it is true.

The hook is one Redis key and one `if` per loop, so the interesting risk is not
the mechanism. It is the enum drifting away from the loops: a member whose loop
never reads its key accepts a pause and does nothing, which is worse than a
refusal because the caller believes the fault landed. Half of this file is
therefore static — it parses `workers/dispatcher.py` and asserts the enum, the
`LOOP_FUNCTIONS` map and the loops `worker_loop` actually starts are the same
set, and that each of those loops really calls `loop_is_paused` with its own
member.

The other half is behavioural, and each test pins a decision that is easy to
undo by accident:

  * the delayed-retry pause must not stop the worker's liveness heartbeat;
  * the outbox-relay pause must sit inside the leader gate, so leadership does
    not move when a replica is paused;
  * the check fails open on a Redis error, and short-circuits with no Redis
    call at all when `CHAOS_ENABLED=false`;
  * a key that goes away — TTL expiry — resumes the loop with no manual step.

`chaos:pause:<loop>` being swept by `make eval-reset` is asserted here too,
against the reset script's own pattern tuple rather than against a copy of it.
"""

from __future__ import annotations

import ast
import asyncio
import fnmatch
import importlib
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import Settings
from app.workers import dispatcher
from app.workers.control_loop_pause import (
    LOOP_FUNCTIONS,
    TICK_INTERVAL_SECONDS,
    ControlLoopName,
    loop_is_paused,
    pause_key_for,
    tick_interval_seconds,
)

_DISPATCHER_SOURCE = pathlib.Path(dispatcher.__file__).read_text(encoding="utf-8")
_DISPATCHER_TREE = ast.parse(_DISPATCHER_SOURCE)


# ---------------------------------------------------------------------------
# Static: the enum against the code
# ---------------------------------------------------------------------------


def _worker_loop_started_loops() -> set[str]:
    """Every module-level `_*_loop` coroutine `worker_loop` starts as a task.

    Derived from the source rather than from a list in a docstring, because the
    docstring is what went stale: the repo's own constitution said "nine
    background loops" and then listed eleven (divergence H1). `worker_loop`
    creating the task is the only statement that makes a loop run.
    """
    worker_loop = next(
        node
        for node in _DISPATCHER_TREE.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "worker_loop"
    )
    module_level_loops = {
        node.name
        for node in _DISPATCHER_TREE.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name.endswith("_loop")
    } - {"worker_loop"}

    started: set[str] = set()
    for node in ast.walk(worker_loop):
        if not isinstance(node, ast.Call):
            continue
        # asyncio.create_task(<callee>(...))
        if getattr(node.func, "attr", None) != "create_task" or not node.args:
            continue
        inner = node.args[0]
        if isinstance(inner, ast.Call):
            name = getattr(inner.func, "id", None)
            if name in module_level_loops:
                started.add(name)
    return started


def test_the_ast_walk_finds_the_loops_at_all() -> None:
    """Sanity floor — every assertion below would pass vacuously on an empty
    set, which is exactly how a drift test rots."""
    assert len(_worker_loop_started_loops()) >= 11


def test_the_enum_is_exactly_the_loops_worker_loop_starts() -> None:
    """The bijection. A twelfth loop cannot ship unpausable, and a member
    cannot outlive the loop it names.

    If this fails after adding a loop: add the enum member, the
    `LOOP_FUNCTIONS` row, the `TICK_INTERVAL_SECONDS` row and the per-tick
    check — not an exemption here. The enum is what `pause_control_loop`
    advertises as the complete set, so an unchecked member is the tool lying.
    """
    assert set(LOOP_FUNCTIONS.values()) == _worker_loop_started_loops()
    assert set(LOOP_FUNCTIONS) == set(ControlLoopName)
    assert len(set(LOOP_FUNCTIONS.values())) == len(LOOP_FUNCTIONS), (
        "two members map to one loop"
    )


def test_the_kafka_consumer_groups_are_not_in_the_enum() -> None:
    """Divergence H2, pinned. `dependency-resolver`, `saga-coordinator` and
    `read-model` are consumer groups, not loops; `kill_consumer` has stopped
    any consumer group since Wave 1. A second mechanism for the same thing
    means two keys and two ways for a teardown to miss one.
    """
    values = {member.value for member in ControlLoopName}
    for group in ("dependency_resolver", "saga_coordinator", "read_model"):
        assert group not in values, (
            f"{group} is a Kafka consumer group — pause it with "
            "kill_consumer(consumer_group=...) instead of adding it here"
        )


def _pause_members_checked_in(loop_name: str) -> set[str]:
    """The `ControlLoopName.X` members one loop coroutine passes to
    `loop_is_paused`."""
    func = next(
        node
        for node in _DISPATCHER_TREE.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == loop_name
    )
    members: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) != "loop_is_paused" or not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Attribute) and getattr(
            arg.value, "id", None
        ) == "ControlLoopName":
            members.add(arg.attr)
    return members


@pytest.mark.parametrize(
    ("member", "loop_name"),
    sorted((m.name, fn) for m, fn in LOOP_FUNCTIONS.items()),
)
def test_each_enumerated_loop_checks_its_own_pause_key(
    member: str, loop_name: str
) -> None:
    """Every member's loop reads that member's key — and only that one.

    This is the assertion that makes the enum honest, and it is static because
    driving eleven loops through a live Redis would test the harness rather than
    the loops.
    """
    assert _pause_members_checked_in(loop_name) == {member}


def test_the_liveness_heartbeat_is_ticked_before_the_pause_check() -> None:
    """`worker_tick()` is the whole worker's heartbeat, and it lives in the
    delayed-retry loop. Pausing one loop must not report the process wedged,
    so the tick has to come first in statement order.
    """
    func = next(
        node
        for node in _DISPATCHER_TREE.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == LOOP_FUNCTIONS[ControlLoopName.DELAYED_RETRY_PROMOTE]
    )
    tick_line = min(
        node.lineno
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "worker_tick"
    )
    pause_line = min(
        node.lineno
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "loop_is_paused"
    )
    assert tick_line < pause_line, (
        "the pause check runs before worker_tick(), so a single-loop pause "
        "would make the deep health check report the worker wedged"
    )


def test_the_mirrored_tick_intervals_match_the_dispatcher() -> None:
    """`TICK_INTERVAL_SECONDS` is a mirror of the dispatcher's own constants
    (the import would be circular). Tripwire, per the house convention."""
    expected = {
        ControlLoopName.OUTBOX_RELAY: dispatcher.OUTBOX_RELAY_INTERVAL,
        ControlLoopName.DELAYED_RETRY_PROMOTE: dispatcher.POLL_INTERVAL,
        ControlLoopName.DLQ_REPLAY_PROMOTE: dispatcher.POLL_INTERVAL,
        ControlLoopName.RESUME_UNBLOCKED_WAITING: dispatcher._RESUME_SWEEP_INTERVAL,
        ControlLoopName.STALE_PENDING_BACKSTOP: (
            dispatcher._STALE_PENDING_SWEEP_INTERVAL
        ),
        ControlLoopName.STALE_RUNNING_SWEEP: dispatcher._STALE_RUNNING_SWEEP_INTERVAL,
        ControlLoopName.LEASE_RENEWAL: dispatcher._RUNNING_LEASE_RENEW_INTERVAL,
        ControlLoopName.METRICS: dispatcher._METRICS_LOOP_INTERVAL,
        ControlLoopName.IDEMPOTENCY_REAPER: (
            dispatcher._IDEMPOTENCY_REAPER_INTERVAL_SECONDS
        ),
    }
    for member, interval in expected.items():
        assert TICK_INTERVAL_SECONDS[member] == float(interval), member.value
    # The two that are settings-derived are declared as such, not guessed.
    assert TICK_INTERVAL_SECONDS[ControlLoopName.DIGEST] is None
    assert TICK_INTERVAL_SECONDS[ControlLoopName.SLO_EVALUATION] is None


def test_a_disabled_slo_loop_reports_no_tick_interval() -> None:
    """`SLO_EVALUATION_INTERVAL_SECONDS=0` is how the demo stack runs, and a
    pause on a loop that is not iterating changes nothing. Say so."""
    with patch(
        "app.workers.control_loop_pause.get_settings",
        return_value=Settings(slo_evaluation_interval_seconds=0, environment="test"),
    ):
        assert tick_interval_seconds(ControlLoopName.SLO_EVALUATION) is None
    with patch(
        "app.workers.control_loop_pause.get_settings",
        return_value=Settings(slo_evaluation_interval_seconds=300, environment="test"),
    ):
        assert tick_interval_seconds(ControlLoopName.SLO_EVALUATION) == 300.0


def test_the_digest_interval_is_clamped_the_way_the_loop_clamps_it() -> None:
    with patch(
        "app.workers.control_loop_pause.get_settings",
        return_value=Settings(llm_digest_interval_hours=0, environment="test"),
    ):
        assert tick_interval_seconds(ControlLoopName.DIGEST) == 60.0
    with patch(
        "app.workers.control_loop_pause.get_settings",
        return_value=Settings(llm_digest_interval_hours=24, environment="test"),
    ):
        assert tick_interval_seconds(ControlLoopName.DIGEST) == 86400.0


# ---------------------------------------------------------------------------
# The key, and the teardown that has to reach it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("member", sorted(ControlLoopName, key=lambda m: m.value))
def test_every_pause_key_lives_under_the_chaos_namespace(
    member: ControlLoopName,
) -> None:
    assert fnmatch.fnmatch(pause_key_for(member), "chaos:*")


def test_make_eval_reset_sweeps_every_pause_key() -> None:
    """Asserted against the reset script's real pattern tuple, not a copy.

    `04:129` says "reset script already sweeps `chaos:*`; verify" — this is the
    verification. A pause key that escaped the namespace would survive the
    reset and stall the next scenario's loop with nothing to correlate it to.
    """
    from tests.unit.test_eval_reset import _reset_module

    patterns = _reset_module()._CHAOS_KEY_PATTERNS
    for member in ControlLoopName:
        key = pause_key_for(member)
        assert any(fnmatch.fnmatch(key, p) for p in patterns), (
            f"{key} matches no pattern in {patterns}"
        )


def test_the_key_helper_accepts_the_enum_and_its_value_identically() -> None:
    assert pause_key_for(ControlLoopName.OUTBOX_RELAY) == "chaos:pause:outbox_relay"
    assert pause_key_for("outbox_relay") == "chaos:pause:outbox_relay"


# ---------------------------------------------------------------------------
# The check itself
# ---------------------------------------------------------------------------


async def test_the_check_does_no_redis_work_when_chaos_is_disabled() -> None:
    """Gate 1 of ADR 0008, on the hot path: production must not pay a Redis
    round-trip per tick per loop for a lab feature."""
    client = AsyncMock()
    with patch(
        "app.workers.control_loop_pause.get_settings",
        return_value=Settings(chaos_enabled=False, environment="test"),
    ), patch("app.core.redis.get_redis_client", return_value=client):
        assert await loop_is_paused(ControlLoopName.OUTBOX_RELAY) is False
    client.get.assert_not_awaited()


async def test_the_check_reads_the_key_when_chaos_is_enabled() -> None:
    client = AsyncMock()
    client.get.return_value = "paused"
    with patch(
        "app.workers.control_loop_pause.get_settings",
        return_value=Settings(chaos_enabled=True, environment="test"),
    ), patch("app.core.redis.get_redis_client", return_value=client):
        assert await loop_is_paused(ControlLoopName.OUTBOX_RELAY) is True
    client.get.assert_awaited_once_with("chaos:pause:outbox_relay")


async def test_the_check_fails_open_when_redis_raises() -> None:
    """A Redis blip must not stall the outbox relay. `_check_chaos_kill` makes
    the same trade for the same reason; the strict variant that fails closed
    exists only to decide a *restart*, and nothing here restarts anything —
    the key's TTL ends the pause.
    """
    client = AsyncMock()
    client.get.side_effect = RuntimeError("redis down")
    with patch(
        "app.workers.control_loop_pause.get_settings",
        return_value=Settings(chaos_enabled=True, environment="test"),
    ), patch("app.core.redis.get_redis_client", return_value=client):
        assert await loop_is_paused(ControlLoopName.OUTBOX_RELAY) is False


# ---------------------------------------------------------------------------
# Behaviour: two loops whose insertion point is load-bearing
# ---------------------------------------------------------------------------


def _paused_then(*values: bool) -> AsyncMock:
    """A `loop_is_paused` stub that answers each tick in turn and then breaks
    the loop, the way the existing loop tests in `test_dispatcher.py` do."""
    return AsyncMock(side_effect=[*values, asyncio.CancelledError()])


async def test_a_paused_delayed_retry_loop_still_ticks_the_heartbeat() -> None:
    factory = MagicMock()
    redis = AsyncMock()
    once = AsyncMock()
    with patch(
        "app.workers.dispatcher.loop_is_paused", new=_paused_then(True)
    ), patch("app.workers.dispatcher._promote_delayed_once", new=once), patch(
        "app.workers.dispatcher.worker_tick"
    ) as tick, patch("asyncio.sleep", new=AsyncMock()):
        await dispatcher._promote_delayed_loop(factory, redis)

    once.assert_not_awaited()
    assert tick.call_count >= 1, "the worker heartbeat stopped with the loop"


async def test_an_unpaused_delayed_retry_loop_still_does_its_work() -> None:
    """The control for the test above: the guard is skipping work because of
    the key, not because the wiring broke."""
    factory = MagicMock()
    redis = AsyncMock()
    once = AsyncMock()
    with patch(
        "app.workers.dispatcher.loop_is_paused", new=_paused_then(False)
    ), patch("app.workers.dispatcher._promote_delayed_once", new=once), patch(
        "app.workers.dispatcher.worker_tick"
    ), patch("asyncio.sleep", new=AsyncMock()):
        await dispatcher._promote_delayed_loop(factory, redis)

    once.assert_awaited_once()


async def test_a_paused_outbox_relay_still_takes_the_leader_gate() -> None:
    """ADR 0020: the pause must hold whichever replica is leader, and must not
    move leadership. Checking in front of the gate would make a paused replica
    stop contending — the pause would still hold (the key is global) but
    leadership would have moved for an unrelated reason.
    """
    entered: list[bool] = []

    class _Gate:
        async def __aenter__(self) -> bool:
            entered.append(True)
            return True

        async def __aexit__(self, *exc: Any) -> None:
            return None

    tick = AsyncMock()
    with patch(
        "app.workers.dispatcher.loop_is_paused", new=_paused_then(True)
    ), patch("app.workers.dispatcher._outbox_relay_tick", new=tick), patch(
        "asyncio.sleep", new=AsyncMock()
    ):
        await dispatcher._outbox_relay_loop(MagicMock(), leader_gate=lambda: _Gate())

    tick.assert_not_awaited()
    assert entered, "the paused relay skipped the leader gate entirely"


async def test_the_relay_resumes_on_its_own_when_the_key_expires() -> None:
    """TTL restores normal publishing with no manual step — the tick that sees
    the key gone does the work."""
    tick = AsyncMock()

    class _Gate:
        async def __aenter__(self) -> bool:
            return True

        async def __aexit__(self, *exc: Any) -> None:
            return None

    with patch(
        "app.workers.dispatcher.loop_is_paused", new=_paused_then(True, False)
    ), patch("app.workers.dispatcher._outbox_relay_tick", new=tick), patch(
        "asyncio.sleep", new=AsyncMock()
    ):
        await dispatcher._outbox_relay_loop(MagicMock(), leader_gate=lambda: _Gate())

    tick.assert_awaited_once()


# ---------------------------------------------------------------------------
# Registration gating
# ---------------------------------------------------------------------------


def test_the_tool_is_absent_from_the_registry_when_chaos_is_disabled() -> None:
    """Gate 1 again, at the surface: the unit tier runs with `CHAOS_ENABLED`
    false, so importing the module must leave `tools/list` unchanged."""
    from app.mcp.registry import list_tools
    from app.mcp.tools.chaos import pause_control_loop  # noqa: F401

    assert "pause_control_loop" not in {t.name for t in list_tools()}


def test_the_tool_declares_the_single_loop_blast_radius_and_the_chaos_scope() -> None:
    """Registration under a chaos-enabled settings object, inspected directly.

    `BlastRadius.SINGLE_LOOP` is a new fifth member of a closed enum, so it is
    a snapshot delta on this tool's description — worth pinning where a reader
    of the tool will look for it.
    """
    from app.core.scopes import Scope
    from app.mcp.chaos import BlastRadius
    from app.mcp.registry import _restore_for_tests, _snapshot_for_tests, list_tools

    snap = _snapshot_for_tests()
    try:
        _restore_for_tests({})
        with patch(
            "app.mcp.chaos.get_settings",
            return_value=Settings(chaos_enabled=True, environment="test"),
        ):
            from app.mcp.tools.chaos import pause_control_loop

            importlib.reload(pause_control_loop)
        td = next(t for t in list_tools() if t.name == "pause_control_loop")
        assert td.required_scope is Scope.CHAOS_INVOKE
        assert td.is_chaos is True
        assert td.description.startswith(
            f"[chaos: {BlastRadius.SINGLE_LOOP.value}] "
        )
    finally:
        _restore_for_tests(snap)
