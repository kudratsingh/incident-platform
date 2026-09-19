"""`kill_consumer(sticky=true)` — a kill that survives its own fix (WO-R3-225, WP-10.0).

The claim half: the two keys, the re-arm, and the one property the packet turns on — the window is
absolute, so restarting the group any number of times cannot extend it. The restart action is
asserted UNCHANGED here, because ADR 0012 rule 1 shipped for exactly that tool and a sticky kill is
the obvious way to reintroduce the leak. The rebless deltas are enumerated at the bottom.
"""

from __future__ import annotations

import fnmatch
import importlib
import json
import pathlib
import time
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
from app.workers import kafka_consumer
from app.workers.kafka_consumer import (
    KILL_FLAG_VALUE,
    kill_key_for,
    sticky_kill_key_for,
)
from pydantic import ValidationError

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

_GROUP = "worker-dispatcher"
_KILL_KEY = kill_key_for(_GROUP)
_STICKY_KEY = sticky_kill_key_for(_GROUP)


@pytest.fixture
def chaos_registered() -> Iterator[None]:
    """`kill_consumer` is chaos-gated, so reload it under patched settings and restore (ADR 0008
    gate 1)."""
    from app.mcp import chaos as chaos_mod
    from app.mcp.tools import chaos as chaos_pkg

    module = chaos_pkg.kill_consumer  # type: ignore[attr-defined]
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


class _Redis:
    """Enough Redis for the kill path: MGET, SET with `ex`/`pxat`, DELETE."""

    def __init__(self, **store: str) -> None:
        self.store: dict[str, str] = dict(store)
        self.sets: list[tuple[str, str, int | None, int | None]] = []
        self.mgets: list[list[str]] = []
        self.raises = False

    async def mget(self, keys: list[str]) -> list[str | None]:
        if self.raises:
            raise ConnectionError("redis saturated")
        self.mgets.append(list(keys))
        return [self.store.get(k) for k in keys]

    async def get(self, key: str) -> str | None:
        if self.raises:
            raise ConnectionError("redis saturated")
        return self.store.get(key)

    async def set(
        self,
        key: str,
        value: str,
        ex: int | None = None,
        pxat: int | None = None,
    ) -> bool:
        self.store[key] = value
        self.sets.append((key, value, ex, pxat))
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self.store.pop(key, None) is not None:
                removed += 1
        return removed


def _chaos_on() -> Any:
    return patch.object(
        kafka_consumer,
        "get_settings",
        return_value=Settings(chaos_enabled=True, environment="test"),
    )


def _ctx(redis: _Redis) -> Any:
    import uuid

    from app.dependencies import Principal
    from app.mcp.registry import ToolContext

    return ToolContext(
        db=None,  # type: ignore[arg-type]
        redis=redis,
        principal=Principal(
            kind="service_account",
            tenant_id=uuid.uuid4(),
            scopes=frozenset({Scope.CHAOS_INVOKE.value}),
        ),
    )


# Gate 1, scope, blast radius — unchanged by the new option


def test_the_hook_is_absent_from_the_registry_when_chaos_is_disabled() -> None:
    from app.mcp.tools.chaos import kill_consumer as _kc  # noqa: F401

    assert "kill_consumer" not in {t.name for t in list_tools()}


def test_the_hook_still_requires_chaos_invoke_and_declares_one_consumer(
    chaos_registered: None,
) -> None:
    spec = get_tool("kill_consumer")
    assert spec is not None
    assert spec.required_scope == Scope.CHAOS_INVOKE
    assert spec.is_chaos is True
    assert spec.description.startswith(
        f"[chaos: {BlastRadius.SINGLE_CONSUMER.value}] "
    )


# The flag defaults off, so no existing scenario moves


def test_sticky_defaults_off(chaos_registered: None) -> None:
    spec = get_tool("kill_consumer")
    assert spec is not None
    inp = spec.input_model(consumer_group=_GROUP)
    assert inp.sticky is False
    assert inp.ttl_seconds == 300


async def test_a_default_call_writes_only_the_kill_flag(
    chaos_registered: None,
) -> None:
    """The regression that matters most: every scenario shipped before this packet calls the hook
    without the flag and must leave exactly the world it left yesterday."""
    spec = get_tool("kill_consumer")
    assert spec is not None
    redis = _Redis()

    out = await spec.handler(
        spec.input_model(consumer_group=_GROUP, ttl_seconds=60), _ctx(redis)
    )

    assert [(k, v, ex) for k, v, ex, _pxat in redis.sets] == [
        (_KILL_KEY, KILL_FLAG_VALUE, 60)
    ]
    assert _STICKY_KEY not in redis.store
    assert out.sticky is False
    assert out.sticky_key is None
    assert out.accepted is True


async def test_a_sticky_call_writes_both_keys_with_the_same_ttl(
    chaos_registered: None,
) -> None:
    spec = get_tool("kill_consumer")
    assert spec is not None
    redis = _Redis()

    out = await spec.handler(
        spec.input_model(consumer_group=_GROUP, ttl_seconds=60, sticky=True),
        _ctx(redis),
    )

    assert [(k, ex) for k, _v, ex, _pxat in redis.sets] == [
        (_KILL_KEY, 60),
        (_STICKY_KEY, 60),
    ]
    assert out.sticky is True
    assert out.sticky_key == _STICKY_KEY
    # The marker's value IS the deadline the output reports, to the second.
    assert float(redis.store[_STICKY_KEY]) == pytest.approx(
        out.expires_at.timestamp(), abs=1.0
    )


async def test_the_kill_flag_lands_before_the_marker(
    chaos_registered: None,
) -> None:
    """Order is the safe direction: a marker that fails to write leaves a plain kill, where a
    marker written first would outlive a kill that never landed and wedge the next scenario."""
    spec = get_tool("kill_consumer")
    assert spec is not None
    redis = _Redis()
    await spec.handler(
        spec.input_model(consumer_group=_GROUP, sticky=True), _ctx(redis)
    )
    assert [k for k, _v, _ex, _pxat in redis.sets] == [_KILL_KEY, _STICKY_KEY]


def test_the_input_is_still_bounded_and_closed(chaos_registered: None) -> None:
    spec = get_tool("kill_consumer")
    assert spec is not None
    schema = spec.input_model.model_json_schema()
    assert schema["properties"]["ttl_seconds"]["maximum"] == 3600
    assert schema["properties"]["ttl_seconds"]["minimum"] == 1
    with pytest.raises(ValidationError):
        spec.input_model(consumer_group=_GROUP, ttl_seconds=0)
    with pytest.raises(ValidationError):
        spec.input_model(consumer_group=_GROUP, ttl_seconds=3601)
    with pytest.raises(ValidationError):
        spec.input_model(consumer_group=_GROUP, unknown_dial=1)


def test_neither_model_carries_a_class_docstring(chaos_registered: None) -> None:
    """A class docstring serializes as the schema `description`, and both schemas reach
    `tools/list` — a contract change disguised as a comment."""
    spec = get_tool("kill_consumer")
    assert spec is not None
    for model in (spec.input_model, spec.output_model):
        assert "description" not in model.model_json_schema(), (
            f"kill_consumer: {model.__name__} has a class docstring"
        )


# The key, and the namespace that makes teardown automatic


def test_the_sticky_key_lives_under_the_chaos_namespace() -> None:
    assert fnmatch.fnmatch(sticky_kill_key_for("any-group"), "chaos:*")


def test_make_eval_reset_sweeps_the_sticky_key() -> None:
    """The reset's own pattern tuple is the authority. This is the teardown the test requirement
    asks for: one sweep takes both keys, so nothing is left to re-arm from."""
    from tests.unit.test_eval_reset import _reset_module

    patterns = _reset_module()._CHAOS_KEY_PATTERNS
    for key in (_KILL_KEY, _STICKY_KEY):
        assert any(fnmatch.fnmatch(key, p) for p in patterns), (
            f"{key} matches no pattern in {patterns}"
        )


# The re-arm


async def test_the_flag_is_re_armed_for_what_is_left_of_the_window() -> None:
    """The whole mechanism in one test: the flag is gone (a restart deleted it), the marker is
    live, so the group reads as killed and the flag is back."""
    marker = f"{time.time() + 30:.3f}"
    redis = _Redis(**{_STICKY_KEY: marker})

    with _chaos_on():
        assert await kafka_consumer._read_kill_state(redis, _GROUP) is True

    assert redis.store[_KILL_KEY] == KILL_FLAG_VALUE
    (key, _value, ex, pxat) = redis.sets[-1]
    assert key == _KILL_KEY
    assert ex is None, "the re-arm must not restart a relative TTL"
    assert pxat == round(float(marker) * 1000)


async def test_a_live_flag_is_not_re_written() -> None:
    """One MGET, and nothing written while the kill is already in force."""
    deadline = time.time() + 30
    redis = _Redis(
        **{_KILL_KEY: KILL_FLAG_VALUE, _STICKY_KEY: f"{deadline:.3f}"}
    )

    with _chaos_on():
        assert await kafka_consumer._read_kill_state(redis, _GROUP) is True

    assert redis.sets == []
    assert redis.mgets == [[_KILL_KEY, _STICKY_KEY]]


async def test_the_window_is_absolute_across_any_number_of_restarts() -> None:
    """WO-R3-225's load-bearing property: the deadline lives in the marker, so re-arming reads it
    rather than recomputing it. Five restarts, one deadline."""
    marker = f"{time.time() + 30:.3f}"
    redis = _Redis(**{_STICKY_KEY: marker})

    pxats: list[int | None] = []
    with _chaos_on():
        for _ in range(5):
            await redis.delete(_KILL_KEY)  # what restart_consumer_group does
            assert await kafka_consumer._read_kill_state(redis, _GROUP) is True
            pxats.append(redis.sets[-1][3])

    assert pxats == [round(float(marker) * 1000)] * 5
    # And the marker itself was never rewritten, so its own Redis TTL still ends the window.
    assert [k for k, _v, _ex, _pxat in redis.sets] == [_KILL_KEY] * 5


async def test_the_window_ends_at_the_deadline_however_it_is_reached() -> None:
    """Time is the other teardown: past the deadline the marker stops re-arming, even while it is
    technically still readable."""
    redis = _Redis(**{_STICKY_KEY: f"{time.time() - 1:.3f}"})

    with _chaos_on():
        assert await kafka_consumer._read_kill_state(redis, _GROUP) is False

    assert redis.sets == []
    assert _KILL_KEY not in redis.store


async def test_a_swept_marker_cannot_re_arm() -> None:
    """`make eval-reset` deletes both keys; this is the state it leaves."""
    redis = _Redis()
    with _chaos_on():
        assert await kafka_consumer._read_kill_state(redis, _GROUP) is False
    assert redis.sets == []


@pytest.mark.parametrize("raw", ["", "soon", "not-a-deadline", "NaN-ish"])
async def test_an_unreadable_marker_releases_the_consumer(raw: str) -> None:
    """Fail open on the marker, like every other flag read here: a corrupted value must not wedge
    a consumer group for the life of the process."""
    redis = _Redis(**{_STICKY_KEY: raw})
    with _chaos_on():
        assert await kafka_consumer._read_kill_state(redis, _GROUP) is False


async def test_nothing_is_re_armed_when_chaos_is_disabled() -> None:
    """ADR 0008 gate 1 for a write rather than a tool. The read still happens — the strict check's
    fail-closed contract depends on it — but production never writes a chaos key."""
    deadline = time.time() + 30
    redis = _Redis(**{_STICKY_KEY: f"{deadline:.3f}"})

    with patch.object(
        kafka_consumer,
        "get_settings",
        return_value=Settings(chaos_enabled=False, environment="test"),
    ):
        assert await kafka_consumer._read_kill_state(redis, _GROUP) is False

    assert redis.sets == []


# Both callers of the read, and the two failure postures that must not move


async def test_the_poll_loop_check_sees_a_sticky_kill() -> None:
    deadline = time.time() + 30
    redis = _Redis(**{_STICKY_KEY: f"{deadline:.3f}"})
    with _chaos_on(), patch("app.core.redis.get_redis_client", return_value=redis):
        assert await kafka_consumer._check_chaos_kill(_GROUP) is True


async def test_the_poll_loop_check_still_fails_open() -> None:
    """A Redis blip must not stop a consumer that nothing killed."""
    redis = _Redis()
    redis.raises = True
    with _chaos_on(), patch("app.core.redis.get_redis_client", return_value=redis):
        assert await kafka_consumer._check_chaos_kill(_GROUP) is False


async def test_the_supervisor_check_sees_a_sticky_kill() -> None:
    deadline = time.time() + 30
    redis = _Redis(**{_STICKY_KEY: f"{deadline:.3f}"})
    with _chaos_on(), patch("app.core.redis.get_redis_client", return_value=redis):
        assert await kafka_consumer._check_chaos_kill_strict(_GROUP) is True


async def test_the_supervisor_check_still_fails_closed() -> None:
    """An unknown kill state must not read as cleared — the property the kill window already had
    and the one a sticky kill leans on hardest."""
    redis = _Redis()
    redis.raises = True
    with _chaos_on(), patch("app.core.redis.get_redis_client", return_value=redis):
        with pytest.raises(ConnectionError):
            await kafka_consumer._check_chaos_kill_strict(_GROUP)


# ADR 0012 rule 1 — the restart action does not learn a new word


async def test_the_restart_action_reports_what_it_did_and_names_no_key() -> None:
    """A restart under a sticky kill DID clear the flag, and says so — `accepted` has never
    asserted that a consumer came back, and the group being still down is read off the resource,
    not off this reply."""
    from app.mcp.tools.actions.restart_consumer_group import (
        RestartConsumerGroupInput,
        restart_consumer_group,
    )

    deadline = time.time() + 30
    redis = _Redis(
        **{_KILL_KEY: KILL_FLAG_VALUE, _STICKY_KEY: f"{deadline:.3f}"}
    )

    out = await restart_consumer_group(
        RestartConsumerGroupInput(
            consumer_group=_GROUP, idempotency_key="sticky-restart-1"
        ),
        _ctx(redis),
    )

    assert out.kill_key_cleared is True
    assert out.accepted is True
    assert "chaos" not in json.dumps(out.model_dump(mode="json")).lower()
    # The sticky marker is NOT this tool's business: a Tier-1 action that reached past the flag
    # into the lab's own state would be the leak at a different angle.
    assert _STICKY_KEY in redis.store

    # ...and the group reads as killed again on the supervisor's next look.
    with _chaos_on():
        assert await kafka_consumer._read_kill_state(redis, _GROUP) is True


def test_the_restart_actions_shape_is_unchanged() -> None:
    """ADR 0012 rule 1 removed `kill_key` / `latency_key` from this output in v0.4.9. This packet
    is the obvious way to put them back, so pin the field set."""
    from app.mcp.tools.actions.restart_consumer_group import (
        RestartConsumerGroupInput,
        RestartConsumerGroupOutput,
    )

    assert set(RestartConsumerGroupInput.model_fields) == {
        "consumer_group",
        "idempotency_key",
    }
    assert set(RestartConsumerGroupOutput.model_fields) == {
        "consumer_group",
        "kill_key_cleared",
        "latency_key_cleared",
        "group_recognized",
        "accepted",
    }


# What the description has to say, because the description is the whole interface


def test_the_description_says_the_group_survives_a_restart(
    chaos_registered: None,
) -> None:
    spec = get_tool("kill_consumer")
    assert spec is not None
    text = spec.description
    assert "`sticky: true`" in text
    assert "restart_consumer_group" in text


def test_the_description_says_the_window_is_absolute(
    chaos_registered: None,
) -> None:
    """The one thing a caller can get wrong: reading the window as rolling would make a scenario's
    recovery time a function of how many times the agent retried."""
    spec = get_tool("kill_consumer")
    assert spec is not None
    text = spec.description
    assert "does not extend it" in text
    assert "expires_at" in text


def test_the_description_says_which_clock(chaos_registered: None) -> None:
    spec = get_tool("kill_consumer")
    assert spec is not None
    assert "the platform's clock" in spec.description


def test_the_output_says_acceptance_is_not_yet_a_stopped_consumer(
    chaos_registered: None,
) -> None:
    spec = get_tool("kill_consumer")
    assert spec is not None
    described = spec.output_model.model_json_schema()["properties"]["accepted"]
    assert "does not confirm" in described["description"]


# ADR 0032


def test_adr_0032_exists_and_is_indexed() -> None:
    adr = (
        _REPO_ROOT
        / "docs"
        / "ADR"
        / "0032-a-sticky-kill-re-arms-and-its-window-is-absolute.md"
    )
    assert adr.is_file(), "ADR 0032 is missing"
    index = (_REPO_ROOT / "docs" / "ADR" / "README.md").read_text(encoding="utf-8")
    assert adr.name in index, "ADR 0032 is not in docs/ADR/README.md"
    assert adr.name in (_REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")


# The deltas, enumerated — the rebless ledger cites this test by name


def test_the_shape_deltas_are_exactly_these(chaos_registered: None) -> None:
    """Where the ledger's field list is pinned: a shape change it does not mention is what makes a
    re-pin surprising."""
    spec = get_tool("kill_consumer")
    assert spec is not None
    assert set(spec.input_model.model_fields) == {
        "consumer_group",
        "ttl_seconds",
        "sticky",
    }
    assert set(spec.output_model.model_fields) == {
        "consumer_group",
        "kill_key",
        "ttl_seconds",
        "sticky",
        "sticky_key",
        "expires_at",
        "accepted",
    }


def test_the_chaos_surface_does_not_grow(
    whole_chaos_surface_registered: None,
) -> None:
    """A flag, not a fourteenth-plus tool: `tools/list` gains no name, so the tool-level rebless
    delta for this packet is empty and the whole delta is field-level."""
    chaos_names = {
        t.name for t in list_tools() if t.required_scope == Scope.CHAOS_INVOKE
    }
    assert "kill_consumer" in chaos_names
    assert len(chaos_names) == 14, sorted(chaos_names)
    assert not [n for n in {t.name for t in list_tools()} if "sticky" in n]


def test_the_option_adds_no_refusal_code_to_the_commanders_chaos_client() -> None:
    """The ChaosClient buckets an unknown `error_code` as a transport fault (R2-16). A bad
    `sticky` refuses as JSON-RPC invalid params, so there is no new code to ledger."""
    import app.mcp.tools.chaos.kill_consumer as module

    assert not [
        name
        for name, obj in vars(module).items()
        if isinstance(obj, type) and issubclass(obj, Exception)
    ], "kill_consumer defines an exception; the ledger would need its code"
