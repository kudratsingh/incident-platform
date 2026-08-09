# ADR 0016 — Defer principal-scoped `tools/list` and blast-radius gate 3 past the eval restart

**Status:** Accepted · **Date:** 2026-08-09 · **Owner:** Platform

> **This ADR defers work rather than doing it, and records two known contradictions it leaves
> standing.** Dated notes pointing here have been added to [ADR 0008](0008-chaos-gating.md) (gate 3
> is specified but not implemented) and [ADR 0012](0012-the-lab-is-invisible-to-the-agent.md)
> (structural invisibility is per-environment, not per-principal). Neither ADR's accepted text was
> rewritten. The point of writing this down is that a future reader can tell a decision from an
> oversight.

## Context

The 2026-08 audit raised two findings against the MCP chaos surface:

**D-05 — `tools/list` is not principal-scoped.** `handle_tools_list` dispatches without the
principal, so in any chaos-enabled environment *every* authenticated principal — including
read-only smoke tokens — sees all 8 chaos tools with full schemas and `[chaos: <blast_radius>]`
description prefixes. A probing `tools/call` then confirms existence via
`missing required scope: chaos:invoke`. ADR 0012's structural invisibility therefore holds only
per *environment*, never per *principal*.

**D-06 — ADR 0008's gate 3 does not exist.** ADR 0008 (Accepted) specifies gate 3 as live
middleware behaviour: "the middleware refuses to dispatch if the current environment's
`CHAOS_MAX_BLAST_RADIUS` is lower … 403 with `error_code: blast_radius_exceeded`". Both
`CHAOS_MAX_BLAST_RADIUS` and `blast_radius_exceeded` appear **only in the ADR**. `chaos.py`
prepends the blast-radius label into the description string and says "informational for now". The
triple gate is a double gate — the same docs/code drift pattern the chaos-safety ADR exists to
prevent, inside the chaos-safety ADR itself.

Both fixes are understood, specced, and small. Neither is being implemented in this campaign.

## Decision

**Defer both past the `v0.5.0` tag *and* past the eval restart.** They become the first items of
the post-restart backlog, not campaign work.

### Why D-05 is deferred

The campaign ends in exactly one contract-snapshot rebless ([ADR 0013](0013-release-before-rerun.md)):
the commander pins the new image by digest, regenerates its 26→27 tool snapshot, and a human
hand-reviews the diff against a ledger of claimed changes. Any unclaimed delta blocks the bless.

D-05 changes **what every principal sees in `tools/list`** — precisely the call that generates the
snapshot. Landing it in the same campaign that performs the one rebless means:

- The snapshot's content becomes a function of which token generated it. Blessing with anything
  but the full 4-scope principal silently truncates the contract, and the failure mode is a
  *smaller* file that still looks well-formed.
- The Tier-1 negative probes on both sides expect `-32002` with `scope` in the message. Masking
  denials as tool-not-found is correct for chaos tools and **wrong** for everything else, because
  the commander's read-only guard negative-probes `mark_dlq_permanent` — a non-chaos Tier-1 tool —
  and hard-fails the smoke run without that error. The masking is safe only with a chaos-only
  constraint the audit's own fix sketch omitted.

Shipping a change that alters the meaning of the rebless, during the campaign whose final act is
the rebless, is self-sabotage. Deferring costs nothing: the pinned `v0.4.9` image the eval consumes
does not contain this change either way.

### Why D-06 is deferred

It depends on D-05. Both edit the same scope-check region of `handle_tools_call`, and gate 3's
ordering relative to the scope check is only decidable once the masking question is settled — gate
3 must sit *after* the scope gate so that a refusal leaks nothing a caller holding `chaos:invoke`
did not already know. Landing gate 3 alone would mean rewriting it when D-05 arrives.

Its own embedded design choice — enforce the cap versus keep the blast-radius label informational —
passes to the post-restart implementer along with the rest.

## Consequences

**What stays true, and is now written down rather than assumed:**

- ADR 0008's gate 3 is **documented but not implemented**. The chaos framework is double-gated in
  code: `CHAOS_ENABLED` (which Terraform refuses to set true in production, and which the
  2026-08 campaign made a real validation rather than a comment) and the `chaos:invoke` scope,
  which is no longer grantable through the self-service API surface at all. Production safety does
  not rest on gate 3 and never has — gate 1 alone keeps the tools unregistered in production.
- ADR 0012's "the lab is invisible to the agent" holds per environment. In a chaos-enabled lab, a
  non-chaos principal can still enumerate the chaos tools. This is an information leak inside the
  lab, not a path to invoking them.

**Cost of the delay.** The leak D-05 describes stays open for the rest of the campaign and through
the first post-restart eval run. That run's principal holds all four scopes, so its `tools/list` is
byte-identical with or without the fix — the deferral does not change what the eval measures.

**Non-goal, stated so it is not rediscovered as a bug:** nobody should "complete cluster P5" by
implementing these. Landing them mid-campaign requires explicit maintainer authorization; a
reviewer's instinct that the cluster looks unfinished is the exact pressure this ADR exists to
resist.

## The post-restart backlog item

Recorded here rather than in a tracker so the constraints survive with the decision. Implement as
one slice, in this order:

1. **Scope-filter `tools/list`** by *all* scopes, not just chaos — one invariant ("you can see
   exactly what you can call"), no special cases in the listing path. Pass the principal into
   `handle_tools_list`; the `AppError` branch in `dispatch` has already returned by that point.
2. **Mask scope denials as tool-not-found for `is_chaos` tools ONLY**, byte-identical in shape to
   the genuinely-unknown-tool branch. Non-chaos denials keep `MCP_FORBIDDEN` /
   `missing required scope: …` — both repos' read-only guard rails depend on it.
3. **Write the `chaos.tool_denied` audit row BEFORE returning not-found**, with
   `denied_by='scope_check'`. The wire lies to the prober; the audit stream keeps the truth. Do not
   refactor this into the unknown-tool branch, which writes a different audit shape.
4. **Then gate 3**: an internal `blast_radius` field on `ToolDefinition` (never on `ToolInfo` or the
   `tools/list` payload — that is contract drift), a `CHAOS_MAX_BLAST_RADIUS` setting defaulting to
   `environment_wide` so existing environments are unchanged, rank comparison after the scope gate,
   and fail-open-with-a-loud-error-log on an unparsable setting value rather than bricking every
   chaos tool on a typo.
5. **Amend ADR 0012 with a Rule 3** (structural invisibility per principal) and **amend ADR 0008**
   to record gate 3 as implemented, replacing the note this ADR added.

**Before consuming any image containing step 1**, the commander side needs a snapshot-script guard
that refuses to write when the tool count is below `len(TOOL_REGISTRY)` — otherwise a
wrong-principal bless silently truncates the contract. That guard is the precondition, not an
optional extra.
