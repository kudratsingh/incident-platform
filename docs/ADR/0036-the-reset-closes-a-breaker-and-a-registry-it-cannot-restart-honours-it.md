# ADR 0036 — The environment reset closes a breaker, and a registry it cannot restart honours it
*Status: Accepted · 2026-09-19 · WO-R3-310 / WO-R3-311 / WO-R3-315*

## Context

`make eval-reset` is the sentence between two scenarios: everything a scenario changed goes back, so
the next world is the world that was graded. Three things it did not put back were found in one week,
all three by the same method — CI's fresh stack disagreeing with a warm volume — and they share one
shape. Each is a piece of state that lives *outside* the namespaces the reset sweeps, and in each case
nothing in the reset's own output said so, so the gap was invisible until a later world read something
nobody had graded.

**The stale-cache fixture key (WO-R3-310).** `cache:jobs:worker-dispatcher:hot_set` is the key
`remediate_stale_cache_success` opens on. `saturate_redis` evicts it — it is the one fixture written
with a TTL, which is precisely what a `volatile-*` eviction policy takes first, and a stack up for
more than a day loses it to the TTL on its own. Every later world then reads `exists: false`. The seed
the reset calls does re-write it, and has since August; what did not exist was any *statement* of that
— no counter in the summary, no test asserting it, and a comment in the commander's Makefile claiming
a re-seed this repository never asserted. The cost was 108 unledgered fixture values on the temporal
worlds mid-rebase, recovered by hand.

**Breaker state (WO-R3-311).** [ADR 0030](0030-breaker-state-is-published-and-a-reading-is-never-invented.md)
publishes each breaker's state to `breaker:state:<name>` under a 24 h TTL, deliberately outside
`chaos:*` so it reads as the platform key it is. The reset's sweep is `chaos:*`, so one
`degrade_downstream` left `bulk-api-sync` reading `open` for a day, in every world recorded after it
— three contaminated fixtures found by #313. And deleting the key is not the fix, twice over. The
registry is a module-level dict in the worker process (ADR 0006), so the breaker that opened still
remembers the failure and writes it straight back at its next publish; and an *absent* record is an
unknown, never a closed breaker (ADR 0030 again), so a world audit that wants to assert "every breaker
closed" has nothing to read. The reset cannot restart the worker, so it needs a way to tell a process
it does not own to forget.

**Open agent runs (WO-R3-315).** [ADR 0035](0035-the-agent-reports-its-run-and-cannot-read-it-back.md)
recorded this one rather than solving it: `agent_runs` rows are closed only by the responder reporting
a terminal state, and the reset is exactly the thing that ends a run without the responder saying so.
So rows accumulated and a run the reset ended stayed open for ever — the one state a console draws as
"still working".

## Decision

**1. Every mutable thing a scenario can leave behind is a step of the reset with a count in its JSON.**
`hot_set_reseeded`, `breakers_reset` and `agent_runs_closed` join the eleven counters already there.
This is the general lesson of WO-R3-310 rather than a detail of it: the reset's summary is what
`make eval-reset` prints and what a readiness note quotes, so a step with no counter is a step nobody
can tell ran. The hot-set check reads the key *before* the seed writes it, so the count reports the
world the reset found, not the one it leaves. A static tripwire
(`test_the_reset_summary_names_every_counter_it_owns`) fails if a step is added without one.

**2. A breaker is reset by rewriting its record closed, not by deleting it.** Each
`breaker:state:<name>` that is not already clean is rewritten `closed` with `failure_count: 0` and
`last_state_change_at` / `last_failure_at` / `last_failure_reason_class` all null — the shape a
breaker that has never failed publishes at boot — carrying over that breaker's own
`failure_threshold` and `recovery_timeout_s`, because a failure count means nothing without the
yardstick beside it. Deleting would leave the reading unable to distinguish a healthy world from an
unreachable store.

**3. The registry honours a signal, and the signal carries a time.** The reset writes
`breaker:reset:at` (an ISO-8601 UTC instant, 24 h TTL, platform namespace) **before** it rewrites any
record, and every `CircuitBreaker` asks for it in two places:

- in `_record`, where a write to Redis was going to happen anyway — so the extra cost is one GET
  beside an existing SET, at most once a minute per breaker while calls flow plus once per state
  change, and ADR 0030's rule that Redis stays out of the hot path holds;
- at the top of `call`, and **only when the breaker is not closed** — so every breaker in a healthy
  world pays nothing, and a breaker holding a fault the reset has cleared stops refusing work
  immediately instead of at its next recovery window.

A breaker that sees a signal it has not seen before clears itself to its boot state, unless its own
`last_failure_at` is *newer* than the signal — in which case the fault belongs to the world now
running and is kept. That comparison is why the signal is a timestamp and not a counter: the question
a breaker has to answer is "is what I remember older than the reset", which a count cannot express,
and a counter-shaped signal wipes a legitimate post-reset failure on the very publish that reports it.
Ordering matters as much as the signal: raised *after* the rewrite, a breaker publishing in between
would write its remembered failure over the clean record and the reset would have made the world worse
than leaving the key alone.

**4. An open `agent_runs` row is closed as `failed`, and never deleted.** Every row with no
`finished_at` gets `state = failed`, `finished_at = now`, and one entry appended to `phase_history`:
`{"state": "failed", "at": …, "closed_by": "reset"}`. `failed` rather than `resolved` or `escalated`
because those two are claims about what the responder concluded and it concluded nothing. The marker
goes in `phase_history` because that list is already the row's own append-only timeline, so a console
draws "failed, closed by the reset" from what is there and no column is added. The responder's own
words — `briefing`, `current_hypothesis`, `last_step`, `scenario` — are left byte-identical, for the
reason the reset never touches `audit_logs`: they are evidence (ADR 0012).

## Alternatives considered

**Delete `breaker:state:*` with the rest of the sweep.** Half a fix: the reading goes from a wrong
answer to no answer, the registry writes the fault back within a recovery window, and the world audit
still cannot assert the state it needs.

**Restart the worker as part of the reset.** It would work and it is not available: the reset runs as
a script inside a container against a stack it does not orchestrate, the commander's runbook calls it
between scenarios on a live stack, and a restart loses the eight consumer groups' in-flight work and
every other breaker's honest state.

**An admin-only internal endpoint the reset calls.** The order offered this. It needs a new authorised
surface, a second copy of the reset's target gate, and reachability from wherever the reset runs — a
lot of machinery, all of it new, to deliver one fact the process already reads Redis for at exactly the
right moments.

**A counter (`breaker:reset:<epoch>`, or an `INCR`ed epoch).** The shape the order sketched. Rejected
on the failure above: it cannot tell a remembered fault from a current one, so a breaker that opens a
second after the reset is closed again by the same signal. A key *per* epoch also costs a scan per
check and leaves two signals both looking current.

**Gate the signal on `CHAOS_ENABLED`.** Tempting — nothing writes the key in production — but it makes
the breaker depend on settings it has never imported, adds a way for the mechanism to be silently off,
and buys nothing on a stack where chaos is always enabled. The read fails open, so an absent key and
an unreachable store cost the same as the gate would.

**Delete the lab's own `agent_runs` rows.** Also offered by the order. Declined: the rows are what the
demo console replays, they carry the responder's own briefing, and "lab-labelled" is not a property
this table has — `scenario` is free text the caller chose.

**Drop the hot set's TTL, so nothing can evict or expire it.** The honest fix for the durability half
of WO-R3-310, and out of scope here: the TTL is a value the commander's canned fixtures and its drift
ledger read back through `get_cache_key_info`, so changing it is a fixture-drift change in the other
repository. Filed as a follow-up instead.

## Consequences

- The reset's JSON grows three counters. The commander parses that JSON; the keys are additive, so
  nothing there breaks, and the comment in its Makefile that claims the hot-set re-seed is now true of
  code that asserts it. Correcting that comment is the commander's half, not this one.
- **No tool surface moves.** No tool name, description, schema, scope or flag changes, so there is no
  contract delta and nothing to rebless.
- One new platform Redis key, `breaker:reset:at`, catalogued in `docs/REDIS.md`. It carries a TTL, so
  `saturate_redis` can evict it and it can reappear with an *older* time than one a breaker has already
  seen. That is safe by construction: the comparison is against the fault's own time, so a stale signal
  cannot close a breaker that is currently failing, and a new one is honoured whatever its order.
- The signal is written by the reset's process and compared in the worker's, so clock skew between them
  shifts the boundary — the same caveat [ADR 0032](0032-a-sticky-kill-re-arms-and-its-window-is-absolute.md)
  records for a sticky kill's deadline, immaterial on one host and bounded by the key's own TTL.
- A race of one instant is accepted: a breaker that fails in the microseconds between the reset reading
  the records and rewriting them has its fresh state overwritten closed. The reset is the last writer
  for the instant it runs, which is what it is for.
- A responder still reporting into a run the reset closed gets `agent_run_already_finished` (409) on its
  next call. That is the truth — its world is gone — and the reporter is fail-open, so it logs and
  carries on.
- A breaker that is refusing calls now does one Redis GET per refusal. Its only caller is a worker
  processor with a bounded fan-out (`MAX_ENDPOINT_COUNT`), and the read fails open, so an unreachable
  store costs a refusal rather than a hang.

## Pointers

- `app/core/breaker_state.py` — `BREAKER_RESET_AT_KEY`, `publish_breaker_reset`,
  `read_breaker_reset_at`, `reset_breaker_states`.
- `app/core/circuit_breaker.py` — `CircuitBreaker._honour_reset`, and the two places it is called.
- `scripts/reset_eval_state.py` — `_reseed_hot_set`, `_reset_breaker_states`,
  `_close_open_agent_runs`, and steps 8–10 of the module docstring.
- `backend/tests/unit/test_breaker_reset.py` — the signal's semantics, including the control that shows
  the registry writing its remembered failure back without it.
- `backend/tests/integration/test_eval_reset_breakers.py` — the end-to-end proof on a real Redis:
  `degrade_downstream` → the shipped breaker opens → the reset → `get_circuit_breakers` reads closed
  from another client, and the registry admits calls again with no restart.
- `backend/tests/unit/test_eval_reset.py` — the hot-set and `agent_runs` steps, and the summary
  tripwire.
