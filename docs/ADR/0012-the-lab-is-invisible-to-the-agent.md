# ADR 0012 — The lab is invisible to the agent

**Status:** Rule 1 accepted + shipped (v0.4.9) · Rule 2 **accepted-deferred, target: post-rerun** · **Date:** 2026 Q3 · **Owner:** Platform

> **Split status, deliberately.** The two rules below share a root cause but not a timeline.
>
> **Rule 1 (non-chaos tools never name chaos internals)** shipped in v0.4.9. It was a correctness fix on a live leak.
>
> **Rule 2 (scenario-owned DLQ fixtures)** is accepted on the merits and **deferred until after the clean-baseline rerun** — operator decision. It is an eval-architecture improvement, not a correctness fix, and landing it now would force another platform version cycle plus an agent-side contract sync immediately before the run it is meant to improve. The standing fixture pool remains the baseline until then.
>
> **Correction (post-merge).** The rule-2 implementation is **on `master`**, not parked on a branch. PR #92 merged shortly before the docs PR that carried this ADR, so the docs branch picked it up. Deliberately left in place rather than reverted — see "What the deferral now means" below. The deferral still holds, but the constraint that enforces it changed from *don't merge* to **don't cut a tag before the rerun**.

## Context

Two independent incidents in the 2026-08-03 campaign share one root cause: the test apparatus was visible to the agent under investigation, and the agent — behaving reasonably — investigated the apparatus.

**1. Tool responses named the chaos rig.** `restart_consumer_group` returned `kill_key: "chaos:kill:worker-dispatcher"` and `latency_key: "chaos:latency:worker-dispatcher"`. That tool requires only `actions:execute`, not `chaos:invoke` — so a principal with no chaos scope at all still learned the chaos framework existed, and what its keys were named. At least one investigation chased the harness instead of the fault.

**2. A standing DLQ fixture pool was always in frame.** The eval seed maintained four `dead_letter` rows, restored between scenarios. Every scenario therefore ran against a populated DLQ regardless of its subject. Commander [ADR 0010](https://github.com/kudratsingh/incident-commander/blob/main/docs/ADR/0010-scenario-owned-dlq-fixtures.md) documents three campaign runs that pivoted onto those rows when their real subject was healthy or absent — including one that fired a real Tier-1 replay and resolved a scenario that expected an escalation.

These look like different bugs. They are the same bug: **the lab leaked into the experiment.** An agent that can see the test rig will reason about the test rig, and every such observation is a wrong-reason pass or a wrong-reason failure. The eval stops measuring the agent and starts measuring the furniture.

## Decision

**The agent's observable surface contains only the system under test, never the apparatus testing it.** Two rules follow.

### 1. Non-chaos tools never name chaos internals

Chaos key names, flag names, and framework vocabulary do not appear in the response or description of any tool that doesn't require `chaos:invoke`.

`restart_consumer_group` drops its `kill_key` and `latency_key` string fields. The `kill_key_cleared` / `latency_key_cleared` booleans stay — they carry the entire operational outcome ("was something actually cleared?") without disclosing that a chaos framework wrote it. Its description no longer narrates chaos either.

> **BREAKING (v0.4.9).** `restart_consumer_group` output loses `kill_key` and `latency_key`. This is a field *removal*, not an addition — a consumer with those fields required will fail to parse. Every other change in v0.4.9 is additive. Agent-side contract snapshots must re-sync. Rationale is the leak above: the fields were never actionable, only revealing.

A regression test asserts the substring `chaos` does not survive anywhere in the tool's payload, so the leak cannot quietly return through a future field.

Note the leak was *not* in `get_redis_health`, which was the initial suspect. That tool runs PING + INFO and never enumerates keys, so it cannot name one. Worth recording because the wrong diagnosis would have produced a no-op fix and left the real leak in place.

### 2. Scenarios declare their own fixtures; the baseline is empty

*(Accepted; implementation deferred to post-rerun — see status note above.)*

Accepting commander ADR 0010. The inter-scenario baseline becomes an empty DLQ. A scenario that needs DLQ content declares it — the same principle PR #54 already established for chaos faults, applied to fixtures.

Platform half, when it lands:

- **`seed_dlq_messages`** — a chaos-gated hook creating N rows with declared `remediation_hint`, `job_type`, `count`, and error string. Chaos-gated rather than a plain seed helper because it writes `dead_letter` rows into a live database; that is fault injection whatever it is named, and it inherits [ADR 0008](0008-chaos-gating.md)'s triple gate so it can never fire in production.
- **`EVAL_EMPTY_DLQ_BASELINE`** — when set, the reset sweep drops its fixture-ID exclusion and clears every `dead_letter` row.
- Rows the hook creates are tagged `payload.seeded_fixture` and **deleted** on reset, not cancelled. The cancel-don't-delete rule exists because swept rows may be real history for a real user; declared scaffolding is not, and cancelling it would accumulate thousands of dead rows across eval runs.

## Sequencing

The baseline flip is a breaking change for every `dlq_*` scenario written against the standing pool, and the two repos deploy independently. Flipping the default in the same change that ships the hook would break the commander's evals in the window between the two merges.

So it lands in three steps, **after the rerun**, and **`EVAL_EMPTY_DLQ_BASELINE` is opt-in, never a default flip in the same change**:

0. **(Now.)** Code is on `master` but inert — see below. The standing pool remains the baseline through the clean-baseline rerun.
1. **Platform.** Ship `seed_dlq_messages` + the opt-in flag. Default behaviour unchanged — the standing pool still exists, existing scenarios keep passing.
2. **Commander.** Migrate `dlq_*` scenarios to declare their fixtures via the hook; run with `EVAL_EMPTY_DLQ_BASELINE=1`.
3. **Platform.** Flip the default, retire `_dlq_specs()` and `_reset_dlq_state`, simplify the sweep to an unconditional clear.

At no point is either side broken by the other's merge. Step 3 is a small follow-up, not a rewrite — the sweep already exists and only loses a condition.

**Why step 0 exists.** The implementation was built and verified before the deferral decision (both modes exercised against a live stack; the mode proved reversible, with `_reset_dlq_state` restoring the pool when the flag is cleared). The deferral is about *when the version cycle lands*, not about doubt over the design.

### What the deferral now means, given the code is on `master`

The rule-2 implementation merged to `master` ahead of the deferral being recorded. It was left in place rather than reverted, because a revert plus a later un-revert buys nothing the release boundary already provides. What matters is which artifact the rerun consumes:

- **The pinned `v0.4.9` image does not contain it.** Verified: that image ships 8 chaos tools, no `seed_dlq_messages`. The rerun runs against the pinned digest, so it sees exactly the surface the agent's contract snapshot was taken against.
- **Behaviour on `master` is unchanged.** `EVAL_EMPTY_DLQ_BASELINE` defaults off, so the standing pool is still the baseline. Verified post-merge: `empty_dlq_baseline: false`, `dlq_swept: 0`, 4 DLQ rows.
- **`master`'s tool surface has drifted by one** — 27 tools instead of 26, because `seed_dlq_messages` registers when `CHAOS_ENABLED=true`. A run against a *dev* stack built from `master` would fail the contract snapshot on that extra tool. A run against the pinned image would not.

So the operative constraint is now: **do not cut a tag before the rerun.** Tagging would build an image containing the new tool, and the commander's pin bump would drag in the contract change the deferral exists to avoid. Merging was never the risk; releasing is.

> **Superseded (2026-08-08).** This operative constraint — and only it — is replaced by
> [ADR 0013](0013-release-before-rerun.md): the 2026-08 fix campaign ships **fixes → new version →
> re-pin → eval**, with the 26→27 tool drift consumed as the planned, ledgered diff of a single
> end-of-campaign rebless. Rules 1 and 2 of this ADR, and rule 2's deferred baseline flip, stand
> unchanged.

## Consequences

**Positive.** Investigation quality becomes attributable to the agent rather than to ambient state. DLQ scenario expectations become exact — declared rows in, graded outcomes against those rows — instead of calibrated against a pool that drifts as scenarios replay and mark rows.

**Negative.** More YAML: a scenario wanting DLQ content must say so. Cross-repo sequencing costs three merges instead of one. And scenarios that *want* a noisy environment must now construct that noise explicitly — which the campaign suggests is worth having as a distractor-resistance family, but is nonetheless more work than inheriting it by accident.

**A rule that now needs enforcing.** "Non-chaos tools never name chaos internals" is currently upheld by one regression test on one tool. Every new Tier-1 action is an opportunity to reintroduce the leak. The durable fix is a registry-level test asserting that no non-chaos tool's schema or description contains chaos vocabulary; sized as a follow-up.

## Alternatives considered

**Redact chaos keys at the serialization layer** rather than removing the fields. Rejected: it keeps a field whose value is always redacted, which is a worse contract than not having the field, and it invites the assumption that redaction is a general safety net when it would only ever cover the patterns someone remembered to list.

**Per-scenario tenant isolation** instead of an empty DLQ baseline (commander ADR 0010's option 3). The clean-room answer, and multi-tenancy could support it — but it multiplies seed time per scenario and complicates the service-account story (scope per tenant per scenario) to solve a problem the empty baseline already solves for the only shared surface that has actually bitten. Revisit if traces or deploy history start contaminating scenarios the same way.

**Keep the fixture pool, document it as expected furniture.** Rejected: it asks the agent to learn which populated DLQ is real and which is scenery. That is not a skill worth training, and it is not a distinction an on-call SRE would ever have to make.
