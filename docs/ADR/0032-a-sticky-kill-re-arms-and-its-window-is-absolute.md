# ADR 0032 — A sticky kill re-arms, and its window is absolute
*Status: Accepted · 2026-09-18 · WO-R3-225 (plan v2.1 WP-10.0; closes WO-R2-165)*

## Context

Every fault this lab can inject is fixed by the agent's first correct action. `kill_consumer` sets
`chaos:kill:<group>`; `restart_consumer_group` deletes it; the supervisor sees it gone and brings the
group back. That is the right shape for a scenario that asks "can the agent find and fix this", and
the wrong shape for the question Phase 10 asks: can the agent notice that its remediation did not
work, reinvestigate, and try a second hypothesis. Nothing in the platform could make a Tier-1 action
fail, so `remediate_verify_fails` was canned-only by design and every retry scenario would have been
too.

What is needed is narrow: one fault that is still there after the action that normally clears it,
for a bounded time, with the action itself still reporting honestly.

Two constraints shaped the answer.

**ADR 0012 rule 1 already shipped for this exact tool.** `restart_consumer_group` used to return
`kill_key` and `latency_key`, which told an investigating agent that a chaos framework had written
its fault. The fields were removed in v0.4.9 and a regression test asserts the substring `chaos`
cannot come back in that tool's payload. A sticky kill is the obvious way to reintroduce the leak at
a different angle — "the restart failed because a chaos key is set" would be the same defect in
prose — so the design had to leave the action's output alone.

**The kill window already fails closed.** `_check_chaos_kill_strict` raises rather than reporting
"cleared" when the lookup fails, because treating an unknown kill state as cleared resurrects the
consumer in the middle of the window a scenario is measuring. That is the vocabulary a sticky kill
needs, and the place it belongs.

## Decision

**`kill_consumer(sticky=true)` writes a second key whose value is an absolute deadline, and the kill
flag is re-armed from it for whatever is left of that window.**

- **Two keys.** `chaos:kill:<group>` is unchanged — same name, same value, same TTL. Beside it,
  `chaos:kill_sticky:<group>` holds the unix deadline `ttl_seconds` gives, with its own TTL set to
  the same instant. Both are under `chaos:*`, so the eval reset's single scan takes both and nothing
  is left to re-arm from.
- **The flag is deleted and found back, not hidden.** `restart_consumer_group` deletes the flag,
  reports `kill_key_cleared: true` — which is what it did — and the next kill-state read re-arms the
  flag before the supervisor restarts anything. The action is not modified, reads nothing new, and
  names nothing new.
- **The re-arm lives in the kill-state read, not beside it.** `_read_kill_state` fetches both keys in
  one MGET and re-arms when the flag is absent and the marker is live, so there is no window in
  which a caller sees "not killed" while the sticky window is open. Both existing callers get it:
  the poll loop's fail-open check (which is what makes a sticky kill survive a worker-process
  restart) and the supervisor's fail-closed one.
- **The window is absolute.** The deadline is stored once, at arm time. The re-arm sets the flag with
  `PXAT` at that instant, never a fresh relative TTL, so restarting the group any number of times
  cannot extend it. Two independent bounds end it: the deadline in the marker's value (the worker's
  clock) and the marker's own TTL (Redis's clock).
- **An unreadable marker releases the group.** A missing, empty, non-numeric, NaN or infinite value
  is not a deadline, and fails open — the same posture as every other flag read here. A corrupted
  marker must not wedge a consumer group for the life of the process.
- **The re-arm is a write, so it is gated.** `_read_kill_state` reads unconditionally, because the
  strict check's fail-closed contract depends on the read happening; the re-arm returns False unless
  `CHAOS_ENABLED` is set ([ADR 0008](0008-chaos-gating.md) gate 1). Nothing writes a chaos key in
  production.
- **No new tool, no new blast radius, no new refusal code.** `sticky` is a flag on an existing hook
  with `single_consumer` already declared, defaulting false, so every scenario shipped before this
  packet leaves exactly the world it left yesterday. A bad value refuses as JSON-RPC invalid params.

## Alternatives considered

**A separate key the restart action does not clear** (the hook writes only `chaos:kill_sticky:*`, and
the consumer checks that instead). Rejected on honesty grounds: the restart would find no flag and
answer `kill_key_cleared: false`, and that tool's own description says a false there with an
unchanged consumer is the signature of a reused `idempotency_key` replaying an old success. The agent
would be told it had made a mistake it had not made — a worse failure than the one this packet is
built to create, because the scenario is grading exactly how the agent reacts to a failed action.

**Teaching `restart_consumer_group` about the sticky key** — refuse, or report "the kill is sticky".
Rejected: it reintroduces the ADR 0012 rule 1 leak, and a Tier-1 action reaching past its own flag
into the lab's state is the lab becoming visible.

**A new `wedge_consumer` hook.** Rejected. The behaviour is `kill_consumer`'s, with one dial; a new
verb would mean a second mechanism, a second key family and a second thing for a teardown to miss
(the reason [ADR 0027](0027-control-loop-pause-closed-enum.md) kept one hook for eleven loops).

**A rolling TTL** — each re-arm grants a fresh `ttl_seconds`. Rejected, and this is the property the
work order names: a world whose recovery time is a function of how many times the agent retried is
not a world a scenario can precondition, and an agent that retried enough would never see recovery
at all.

## Consequences

- **`restart_consumer_group` now has a truthful reply that is easy to misread as success.** Its
  description already said `accepted` does not assert that anything restarted and that recovery must
  be confirmed through group membership; that sentence is now load-bearing rather than cautionary.
  Graders must read the group's own state, never the action's reply — the same rule the reused-
  idempotency-key lesson produced (`PURGE_IDEMPOTENCY=1` between attempts, because a repeated
  restart under one key returns a perfect success that did nothing).
- **The deadline crosses a process boundary.** The hook computes it in the MCP process; the re-arm
  compares it in the worker process. Both run from the same image on the same host here, so the skew
  is not material, and the marker's own Redis-side TTL bounds the window regardless of what the
  worker's clock believes. Recorded rather than solved.
- **Teardown is unchanged for operators of the reset**: one `chaos:*` scan, no new pattern. A sticky
  kill left behind by a crashed run ends on its own deadline.
- **Flipping `remediate_verify_fails` to live is a separate decision.** This ADR makes it possible.
  It needs its own precondition, its own first-paid-run review, and the ADR 0008 single-attempt
  claims revisited on the commander side; it is not bundled here.

## Pointers

- `backend/app/mcp/tools/chaos/kill_consumer.py` — the hook and the `sticky` dial
- `backend/app/workers/kafka_consumer.py` — `sticky_kill_key_for`, `_rearm_sticky_kill`,
  `_read_kill_state`, and the two checks that call it
- `backend/app/workers/dispatcher.py` — `_supervise_consumer`'s kill window
- `backend/tests/unit/test_kill_consumer_sticky.py` — the claims, and the rebless deltas
- `backend/tests/integration/test_sticky_kill_survives_restart.py` — a real Redis, a real
  supervisor, a real restart action
- [ADR 0008](0008-chaos-gating.md), [ADR 0009](0009-consumer-lifecycle-and-supervision.md),
  [ADR 0012](0012-the-lab-is-invisible-to-the-agent.md)
