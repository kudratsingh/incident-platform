"""The reset signal a breaker registry honours without being restarted (WO-R3-311, ADR 0036).

Deleting `breaker:state:<name>` is not a reset. The registry is a module-level dict in the
worker (ADR 0006/0030), so the breaker that opened still remembers the failure and writes it
back over the clean record at its next publish — which is why one `degrade_downstream`
contaminated every later recording for the record's whole 24 h TTL. The fix is a signal the
environment reset raises and every breaker honours before it speaks again.

The signal carries a *time*, and these tests are where that matters: a breaker must forget what
it remembers from before the reset and keep a failure that happened after it. A counter cannot
answer that question, which is what `test_a_failure_after_the_signal_stands` pins.

`test_the_registry_republishes_its_remembered_failure_without_the_signal` is the bug, kept as the
control: it passes before this change and after it, and it is the reason the signal exists.
"""

from __future__ import annotations

import fnmatch
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.core import breaker_state
from app.core.breaker_state import (
    BREAKER_RESET_AT_KEY,
    BREAKER_STATE_CLOSED,
    BREAKER_STATE_KEY_PREFIX,
    BREAKER_STATE_TTL_SECONDS,
    breaker_key_for,
    publish_breaker_reset,
    read_breaker_reset_at,
    read_breaker_states,
    reset_breaker_states,
)
from app.core.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState


class _FakeRedis:
    """Enough Redis for the state records and the signal, with an operation log.

    The log is what pins ordering: the signal has to be raised before any record is
    rewritten, and a closed breaker must not read it on every call.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.ops: list[str] = []
        self.fail = fail

    def _guard(self) -> None:
        if self.fail:
            raise ConnectionError("no route to the store")

    async def get(self, key: str) -> str | None:
        self.ops.append(f"get {key}")
        self._guard()
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.ops.append(f"set {key}")
        self._guard()
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def scan(
        self, cursor: int = 0, match: str = "*", count: int = 100
    ) -> tuple[int, list[str]]:
        self.ops.append("scan")
        self._guard()
        return 0, [k for k in self.store if fnmatch.fnmatch(k, match)]


async def _boom() -> None:
    raise ConnectionError("upstream refused")


async def _ok() -> str:
    return "ok"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    """The store both halves of the breaker reach when no client is passed."""
    client = _FakeRedis()
    monkeypatch.setattr(breaker_state, "_client", lambda: client)
    return client


async def _open_breaker(name: str = "bulk-api-sync") -> CircuitBreaker:
    breaker = CircuitBreaker(name, failure_threshold=1, recovery_timeout=30.0)
    with pytest.raises(ConnectionError):
        await breaker.call(_boom)
    assert breaker.state is CircuitState.OPEN
    return breaker


def _record(fake: _FakeRedis, name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(fake.store[breaker_key_for(name)])
    return loaded


# The key, and what it is not


def test_the_signal_is_a_platform_key_outside_the_lab_namespace() -> None:
    """Like `breaker:state:*` itself (ADR 0030), so the `chaos:*` sweep does not carry it
    and the reset raising it is a deliberate step rather than a side effect."""
    assert BREAKER_RESET_AT_KEY.startswith("breaker:reset:")
    assert not BREAKER_RESET_AT_KEY.startswith("chaos:")
    assert not fnmatch.fnmatch(BREAKER_RESET_AT_KEY, f"{BREAKER_STATE_KEY_PREFIX}*")


def test_the_closed_state_string_is_the_registrys_own() -> None:
    """The reset writes this string into a record the registry also writes, so the two
    spellings cannot drift apart."""
    assert BREAKER_STATE_CLOSED == CircuitState.CLOSED.value


async def test_raising_the_signal_records_when_and_bounds_it(fake: _FakeRedis) -> None:
    before = datetime.now(UTC)
    raised = await publish_breaker_reset(fake)

    assert raised is not None
    assert before <= raised <= datetime.now(UTC)
    assert fake.ttls[BREAKER_RESET_AT_KEY] == BREAKER_STATE_TTL_SECONDS
    assert await read_breaker_reset_at(fake) == raised


async def test_an_unreachable_store_reports_no_signal_rather_than_raising() -> None:
    """Read fails open: a breaker that cannot ask keeps the state it has, because the
    alternative is a diagnostic that breaks calls."""
    assert await read_breaker_reset_at(_FakeRedis(fail=True)) is None
    assert await read_breaker_reset_at(_FakeRedis()) is None  # absent, not an error


async def test_an_unparseable_signal_is_no_signal(fake: _FakeRedis) -> None:
    fake.store[BREAKER_RESET_AT_KEY] = "whenever"
    assert await read_breaker_reset_at(fake) is None


# The registry honours it, without a restart


async def test_the_registry_republishes_its_remembered_failure_without_the_signal(
    fake: _FakeRedis,
) -> None:
    """The bug, as the control. Delete the record and let the breaker speak again: the
    in-process registry writes the same open state back, which is why `breaker:state:*`
    survived every reset for the record's whole TTL."""
    breaker = await _open_breaker()
    del fake.store[breaker_key_for("bulk-api-sync")]

    await breaker._record(changed=True)  # the next publish, whatever prompts it

    assert _record(fake, "bulk-api-sync")["state"] == CircuitState.OPEN.value


async def test_a_breaker_forgets_its_failure_when_the_signal_is_newer(
    fake: _FakeRedis,
) -> None:
    """The fix: the next publish honours the signal first, so the record it writes is
    closed with the failure fields null — and the registry itself is closed too."""
    breaker = await _open_breaker()
    await publish_breaker_reset(fake)

    await breaker._record(changed=True)

    assert breaker.state is CircuitState.CLOSED
    written = _record(fake, "bulk-api-sync")
    assert written["state"] == BREAKER_STATE_CLOSED
    assert written["failure_count"] == 0
    assert written["last_failure_at"] is None
    assert written["last_failure_reason_class"] is None
    assert written["last_state_change_at"] is None


async def test_an_open_breaker_admits_calls_again_without_a_process_restart(
    fake: _FakeRedis,
) -> None:
    """The half a deleted key cannot buy: the world is only reset if the breaker stops
    refusing work, and a restart is not available to `make eval-reset`."""
    breaker = await _open_breaker()
    with pytest.raises(CircuitOpenError):
        await breaker.call(_ok)

    await publish_breaker_reset(fake)

    assert await breaker.call(_ok) == "ok"
    assert breaker.state is CircuitState.CLOSED


async def test_a_failure_after_the_signal_stands(fake: _FakeRedis) -> None:
    """Why the signal carries a time. A breaker that fails *after* the reset must stay
    open: the fault belongs to the scenario now running, and a counter-shaped signal would
    wipe it on the very publish that reports it."""
    await publish_breaker_reset(fake)
    breaker = CircuitBreaker("bulk-api-sync", failure_threshold=1, recovery_timeout=30.0)

    with pytest.raises(ConnectionError):
        await breaker.call(_boom)

    assert breaker.state is CircuitState.OPEN
    written = _record(fake, "bulk-api-sync")
    assert written["state"] == CircuitState.OPEN.value
    assert written["failure_count"] == 1
    assert written["last_failure_reason_class"] == "connection"


async def test_one_signal_clears_once_so_the_next_fault_is_reported(
    fake: _FakeRedis,
) -> None:
    """A reset clears what came before it, not what comes after, and it is not an off
    switch: the same signal must not keep closing a breaker that opens again under it."""
    breaker = await _open_breaker()
    await publish_breaker_reset(fake)
    await breaker._record(changed=True)
    assert breaker.state is CircuitState.CLOSED

    with pytest.raises(ConnectionError):
        await breaker.call(_boom)

    assert breaker.state is CircuitState.OPEN
    assert _record(fake, "bulk-api-sync")["state"] == CircuitState.OPEN.value


async def test_a_signal_older_than_the_fault_is_not_a_reset(fake: _FakeRedis) -> None:
    """The key carries a TTL and `saturate_redis` evicts keys that carry one, so a signal
    can reappear with an older time. It is compared against the fault, not against the
    last signal, so a stale one cannot close a breaker that is currently failing."""
    breaker = await _open_breaker()
    stale = datetime.now(UTC) - timedelta(hours=1)
    fake.store[BREAKER_RESET_AT_KEY] = stale.isoformat()

    await breaker._record(changed=True)

    assert breaker.state is CircuitState.OPEN
    assert _record(fake, "bulk-api-sync")["state"] == CircuitState.OPEN.value


async def test_a_closed_breaker_does_not_ask_the_store_on_every_call(
    fake: _FakeRedis,
) -> None:
    """ADR 0030 kept Redis out of the hot path. The signal is read where a record was
    already about to be written, so a healthy breaker under load pays nothing extra."""
    breaker = CircuitBreaker("quiet", failure_threshold=3, recovery_timeout=30.0)
    for _ in range(5):
        assert await breaker.call(_ok) == "ok"

    assert len([op for op in fake.ops if op.startswith("get")]) == 1
    assert len([op for op in fake.ops if op.startswith("set")]) == 1


async def test_an_unreadable_store_leaves_the_breaker_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure that must not become a reset: a store that cannot be reached says
    nothing, so an open breaker stays open rather than being cleared by an outage."""
    dead = _FakeRedis(fail=True)
    monkeypatch.setattr(breaker_state, "_client", lambda: dead)
    breaker = await _open_breaker()

    await breaker._record(changed=True)

    assert breaker.state is CircuitState.OPEN


# What the reset itself does


async def test_reset_rewrites_an_open_record_as_closed_and_counts_it(
    fake: _FakeRedis,
) -> None:
    await _open_breaker("bulk-api-sync")
    clean = CircuitBreaker("quiet", failure_threshold=3, recovery_timeout=30.0)
    await clean._record(changed=True)

    assert await reset_breaker_states(fake) == 1

    records, unknown = await read_breaker_states(fake)
    assert unknown is None
    assert {r.name for r in records} == {"bulk-api-sync", "quiet"}
    for record in records:
        assert record.state == BREAKER_STATE_CLOSED
        assert record.failure_count == 0
        assert record.last_failure_at is None
        assert record.last_failure_reason_class is None
        assert record.last_state_change_at is None


async def test_reset_keeps_the_yardsticks_a_reading_is_judged_against(
    fake: _FakeRedis,
) -> None:
    """`failure_count` means nothing without `failure_threshold` beside it, so the reset
    restores the state and leaves the breaker's own shape alone."""
    await _open_breaker("bulk-api-sync")

    await reset_breaker_states(fake)

    written = _record(fake, "bulk-api-sync")
    assert written["failure_threshold"] == 1
    assert written["recovery_timeout_s"] == 30.0
    assert written["name"] == "bulk-api-sync"


async def test_reset_is_a_no_op_on_a_world_already_clean(fake: _FakeRedis) -> None:
    """The reset's standing promise: a second run reports zeros."""
    await _open_breaker()
    assert await reset_breaker_states(fake) == 1
    assert await reset_breaker_states(fake) == 0


async def test_reset_raises_the_signal_before_it_rewrites_a_record(
    fake: _FakeRedis,
) -> None:
    """Ordering is the whole guarantee. Raised second, a breaker publishing in between
    would write its remembered failure over the clean record and the reset would have made
    the world worse than leaving the key alone."""
    await _open_breaker()
    fake.ops.clear()

    await reset_breaker_states(fake)

    first_rewrite = next(
        i
        for i, op in enumerate(fake.ops)
        if op.startswith(f"set {BREAKER_STATE_KEY_PREFIX}")
    )
    signal = next(i for i, op in enumerate(fake.ops) if op == f"set {BREAKER_RESET_AT_KEY}")
    assert signal < first_rewrite


async def test_reset_raises_the_signal_even_when_no_breaker_published(
    fake: _FakeRedis,
) -> None:
    """A record can expire or be evicted while the registry still remembers the failure,
    so the signal is not conditional on finding one."""
    assert await reset_breaker_states(fake) == 0
    assert await read_breaker_reset_at(fake) is not None


async def test_reset_survives_a_store_it_cannot_reach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reset has destructive work after this step; a diagnostic store must not stop
    it. Nothing is claimed — the count is zero."""
    dead = _FakeRedis(fail=True)
    monkeypatch.setattr(breaker_state, "_client", lambda: dead)

    assert await reset_breaker_states(dead) == 0
