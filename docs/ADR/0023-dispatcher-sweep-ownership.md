# ADR 0023 — A dispatcher sweep only acts on a row it can prove it owns, and only once per window

**Status:** Accepted · **Date:** 2026 Q3 · **Owner:** Platform
**Amends:** [ADR 0019](0019-stale-running-recovery-sweep.md) §3 and [ADR 0021](0021-bounded-execution-and-non-blocking-dispatch.md) §2 — the in-flight exclusion is no longer the *only* thing that keeps the sweep off live work.

## Context

The worker runs two backstop sweeps over `jobs`. Both were written against a
single-replica mental model, and both express it the same way: they decide
what to do from a scan, act on rows they have no claim to, and keep no record
of having acted.

**The stale-PENDING backstop re-published without limit.** Its predicate is
`status='pending' AND updated_at < now() - 300s`, its action is an outbox
insert, and the action changes nothing the predicate reads. A row that matched
once matched again on the next pass, and the pass after that:

```sql
SELECT * FROM jobs WHERE status='pending' AND updated_at < :cutoff LIMIT 100;
-- ...INSERT INTO outbox_events (...);   -- and nothing else
```

The backstop exists for two real crash windows, so it cannot simply be made
one-shot. But the condition it fires on — a PENDING job with no Redis timer —
is *also* what a dispatcher that is merely behind looks like: the
`job.submitted` is real, just not consumed yet. The sweep's own docstring
already conceded that false positive as tolerable, because the atomic claim
(`claim_for_running`) means the duplicate delivery executes nothing. What it
did not account for is the *rate*: one duplicate per 60s per stale job, for
the entire duration of the lag, on the same topic the dispatcher is already
behind on. The recovery mechanism's failure mode was to deepen the outage it
was reacting to.

**The stale-RUNNING sweep dead-lettered other replicas' work.** Its one
exclusion for live work was `dispatcher.in_flight_job_ids` — a Python `set` on
the consumer object. That answers "is this job mine?" and nothing else. Every
job every *other* replica was executing looked exactly like a crash orphan,
so with more than one replica — steady state on ECS, and unavoidably during
any rolling deploy — replica A dead-lettered replica B's running job and fired
a real `job.dlq` for it. That event is not cosmetic: it fans out to triage, the
saga coordinator, the event log and the read model, so a healthy job acquired a
DLQ entry, an LLM triage row and possibly a saga compensation while its
processor was still running and about to write its own terminal status.

Three properties combine to make this worse than a stray event:

1. the sweep's threshold (900s) is long, so the jobs it reaps are precisely
   the expensive long-running ones;
2. recovery is `DEAD_LETTER`, deliberately terminal and deliberately
   un-republished (ADR 0019), so the "recovery" destroys a job that needed
   none;
3. the write is unconditional, so even the window between the sweep's own scan
   and its own write is unguarded.

**A leader gate does not fix either of these**, which is the tempting answer
and the wrong one. Gating the stale-RUNNING sweep to one replica leaves that
replica with the same blind spot for every job it does not hold — it changes
which replica does the wrong thing, not whether the wrong thing is done. And
gating the stale-PENDING sweep changes nothing at all: one replica publishing
every 60s forever is the bug.

## Decision

### 1. The stale-PENDING backstop stamps what it re-published

`jobs.requeued_at` is written in the **same transaction** as the outbox
insert, and the predicate gains `(requeued_at IS NULL OR requeued_at <
cutoff)`. The row therefore leaves its own predicate for one cutoff window
(300s) and re-enters afterwards.

Three properties, each load-bearing:

- **Same transaction, not a second write.** Either the event and the memory of
  it both land or neither does. A sweep that published without recording it
  would resume duplicating; one that recorded without publishing would strand
  the job for a window.
- **A new column, not a bump of `updated_at`.** `updated_at` *is* the
  staleness signal, and it is rendered in the DLQ list and the trace views. A
  backstop write that moved it would reset the operator-visible age every
  window and make "the sweep noticed" indistinguishable from "the job made
  progress".
- **Suppression expires.** The backstop's whole reason to exist is that a
  `job.submitted` can be lost outright. If the re-published one is lost too,
  the next window must try again — a permanent one-shot would trade unbounded
  duplicates for a permanently stranded job, which is the worse failure.

The alternative of a uniqueness constraint on unpublished `(topic, key)` outbox
rows was rejected: the key is `tenant_id:user_id`, not the job, so it would
collapse unrelated jobs of the same user into one publishable row.

### 2. The stale-RUNNING sweep reads a lease, not a local set

`jobs.heartbeat_at` is renewed every `_RUNNING_LEASE_RENEW_INTERVAL` (20s) by
`_renew_running_leases_loop` in whichever replica holds the job, and a lease
renewed within `_RUNNING_LEASE_TTL_SECONDS` (120s) makes the row invisible to
the sweep. This is the cross-replica form of the question the in-flight set
was being asked, and the only form of it that another process can answer.

- **Six renewals fit inside one TTL.** A blip, a slow pass or a GC pause
  cannot expire a healthy worker's lease. Widening the TTL further only delays
  real crash recovery, which the 900s age threshold already dominates.
- **`heartbeat_at IS NULL` reads as stale.** A crash before the first check-in
  is exactly what the sweep exists to reclaim, so "no lease" must not mean
  "protected". The migration backfills `NOW()` for rows already RUNNING so the
  deploy itself does not look like a fleet-wide crash.
- **Renewal stops at `threshold + grace`.** A wedged worker still runs its
  renewal loop, so an unconditional check-in would let it defend its own stuck
  job forever — re-creating through the lease the single unreclaimable state
  [ADR 0021](0021-bounded-execution-and-non-blocking-dispatch.md) removed. The
  bound is the same one the in-flight exclusion uses, so the two lapse
  together.
- **The in-flight set stays, alongside the lease.** It costs no database
  round-trip and it is still correct when the renewal loop is itself the thing
  that is wedged. Where the two disagree about one of our own rows, the local
  answer is the conservative one.

### 3. The recovery write is a compare-and-set

`JobRepository.update_status` gains a keyword-only `guard`: extra predicates
ANDed into the `WHERE`, and on a non-match it writes nothing, emits nothing,
cascades nothing and returns `None`. The sweep passes the `status`,
`started_at` and `heartbeat_at` its scan observed.

The scan and the write are separate transactions with per-row work in between,
and two things can happen in that gap: the executing replica renews its lease,
or the job settles and is replayed into a *new* RUNNING attempt. A re-read
cannot close either — only doing the check and the write in one statement can.
`started_at` is what distinguishes the attempt the scan saw from the one in
front of the write.

The guard is a mode of `update_status` rather than a second method because the
[ADR 0001](0001-outbox-vs-cdc.md) addendum makes terminal-event
emission non-elective: every path that writes a terminal status goes through
that method, and a conditional terminal write has to be a mode of it rather
than a copy of it that can drift.

## Consequences

**The worker gains a tenth background loop and a steady write.** Every RUNNING
job takes one `UPDATE` every 20s, batched into a single statement per pass, so
the cost is one statement per worker per 20s regardless of concurrency. The
statement pins `updated_at` to its own value so the ORM's `onupdate` does not
fire — a check-in is not progress, and letting it move `updated_at` would churn
a column operators read and make a job wedged for an hour look freshly touched.

**A rolling deploy from a pre-lease version is still exposed, briefly.** An old
replica does not renew, so once the migration's backfill ages out its jobs read
as unleased to a new replica. The exposure is strictly smaller than today's —
the job must *also* be past the 900s threshold, which was already sufficient to
reap it — but it is not zero, and the honest mitigation is a deploy that does
not straddle the migration for more than a lease TTL.

**`requeued_at` is a new column nothing reads outside the backstop.** It is
deliberately not surfaced in the API or the DLQ view. If "how many times has
the backstop had to rescue this job?" becomes an operational question, the
answer is a counter, not this timestamp; tracked in
[ROADMAP.md](../ROADMAP.md) rather than pre-built here.

**`update_status` now has a mode where it silently does nothing.** A guarded
refusal and a missing row both return `None`, which is the same answer to the
same question and keeps the signature honest, but a caller that passes `guard`
and ignores the return value has written a bug the type checker cannot see.
The one call site logs the refusal at INFO with the observed `started_at`, so
a sweep that is being refused constantly is visible rather than silent.

**Neither sweep is leader-gated, and that stays deliberate.** The stale-PENDING
backstop is now idempotent within its window by construction, and the
stale-RUNNING sweep is guarded by a lease plus a CAS; concurrent replicas cost
redundant scans, not wrong writes. Adding a gate would suppress the scans and
weaken nothing, but it would also make the CAS look optional, and the CAS is
the guarantee that must not be removed.
