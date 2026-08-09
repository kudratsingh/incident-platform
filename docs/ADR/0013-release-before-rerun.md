# ADR 0013 — Release before rerun: the campaign ships v0.5.0 first, then the eval runs

**Status:** Accepted · **Date:** 2026-08-08 · **Owner:** Platform (maintainer decision)

> **Supersedes one operative constraint, openly.** The working note in `CLAUDE.md` ("Don't cut a
> tag before the clean-baseline rerun") and the matching "operative constraint" paragraph at the
> end of [ADR 0012](0012-the-lab-is-invisible-to-the-agent.md) are replaced by the ordering below.
> Both were written when the next eval run was imminent and the fix campaign was not; the maintainer
> reversed the ordering on 2026-08-08 when the eval was frozen for the duration of the campaign.
> ADR 0012's two rules themselves are untouched — only the *release timing* constraint moves.

## Context

The 2026-08-08 audit found 129 verified defects across this repo and `incident-commander`,
including two Criticals here (an unauthenticated privilege-escalation chain; silent Kafka message
loss on all consumer groups) and the cluster of lab-hygiene bugs that the audit ranks as the top
cause of the live eval suite being red. The maintainer's decision: **the eval is shut down until
every fix is merged.** Then the platform cuts a new version, the commander re-pins to it, and only
then does the eval run — and that run becomes the new baseline.

The old rule this collides with said the opposite: don't cut a tag until a clean-baseline rerun
has passed. Its rationale was concrete: `master` serves 27 MCP tools where the pinned `v0.4.9`
image serves 26 (the inert `seed_dlq_messages` from ADR 0012 rule 2), so any new tag forces an
agent-side contract re-sync, and an unplanned re-sync right before a measurement run was exactly
the churn the deferral existed to avoid.

Both rules protect the same thing — the integrity of the next measurement — but they cannot both
hold while the eval is frozen: no rerun can precede the tag if no rerun may happen until the fixes
(which include contract-adjacent description changes) are merged and released.

## Decision

The campaign ordering is: **fixes → new version → re-pin → eval.**

1. All campaign work orders merge to `master` under standard PR discipline, verified by unit/API
   tests, lint, types, and static config checks only — the eval runner never executes.
2. The repo owner cuts and pushes **tag v0.5.0** (the tag remains a human act; agent autonomy ends
   at "last fix PR merged").
3. The commander re-pins to the new image **by digest** and reblesses its contract snapshot in one
   planned, hand-reviewed re-sync PR. The 26→27 tool drift and the campaign's enumerated
   description deltas are the *expected* diff of that rebless, checked against a ledger compiled
   from the platform PRs that made them.
4. Only then does the eval run, live, against the pinned v0.5.0 stack — and that accepted run is
   blessed as the new baseline.

### Why the old rule's rationale no longer bites

The old rule treated the contract re-sync as an accident to be prevented. Under the new order it
is a **scheduled, single-PR, end-of-campaign event** with a fully enumerated expected diff:
`+seed_dlq_messages` (flag-off) plus exactly the description changes the campaign's lab-vocabulary
sweep claims. An unclaimed delta at rebless time blocks the bless and opens a platform issue. The
re-sync is no longer something a tag *forces on* the commander mid-flight; it is the mechanism by
which five phases of platform fixes reach it at all.

### What is lost, and accepted

**Pre-tag live validation.** v0.5.0 is cut on unit/API-test evidence alone — the per-work-order
fail-at-HEAD test standard substitutes for the deleted pre-tag live rerun. A bug observable only
against a live stack may therefore be discovered only at the post-release eval, at which point it
costs a `v0.5.1` patch tag plus a second digest bump and rebless cycle. The maintainer accepts
this: it is normal trunk-based release flow, and the alternative — holding 46 merged fixes
unreleased behind a rerun that is itself frozen — deadlocks the campaign.

## Consequences

**Positive.** The campaign has a clean rulebook before any colliding PR merges: every fix lands,
the release is a deliberate gate with named preconditions (SHA-pinned release workflow, quiescent
tool surface, green suites), and the first post-campaign eval measures the artifact users of the
fixes will actually run, not a hybrid. The rerun-then-tag deadlock is dissolved in writing rather
than worked around silently.

**Negative.** A live-red discovery moves from before the tag to after it, with the v0.5.1 remedy
cost above. The window between tag and rebless also briefly widens the set of things "released but
not yet consumed" — bounded by Phase 6 being the very next step.

**Unchanged.** ADR 0012's rules stand: the lab stays invisible to the agent, rule 2's baseline
flip remains deferred (`EVAL_EMPTY_DLQ_BASELINE` stays opt-in and off; the standing fixture pool
remains the baseline for the rerun). The commander's trust anchor remains the image digest, never
a tag ref. The prohibition on cutting tags casually also stands — the tag is a named campaign
phase with preconditions, reserved to the repo owner, not a routine act this ADR liberalizes.
