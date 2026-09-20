# ADR 0038 — A probe the lab makes on the agent's token is labelled by the lab, and the label needs the lab's own credential

**Status:** Accepted · **Date:** 2026-09-20 · **Owner:** Platform · Amends [ADR 0012](0012-the-lab-is-invisible-to-the-agent.md) · Supersedes one paragraph of [ADR 0037](0037-a-run-record-carries-the-run.md)

## Context

The live demo's third take (2026-09-20, archive `f8a13135c6a1`) produced a `/demo` page that was wrong about two things, and both were the platform's fault rather than the console's.

**The action ledger showed reads the agent never made.** After the reset boundary, seven rows appeared under the AGENT principal — `mark_dlq_permanent … INVALID`, `search_traces`, `list_active_alerts`, `get_cache_key_info`, `list_dlq_messages` twice, `get_consumer_lag` — and to anything downstream they were indistinguishable from the agent's own investigation. They came from two evaluator callers that wear the agent's token **on purpose**:

- `evals/guards.py` proves the agent's principal is the right one before a paid run: a Tier-1 attempt that must be refused for want of `actions:execute`, a chaos attempt that must be refused for want of `chaos:invoke`. Those refusals are only meaningful under the token being asserted about.
- `evals/world_audit.py` reads the world exactly as the agent would read it, so that what it records is what the agent would have seen.

Neither can simply use another principal: the principal *is* the subject. So the rows were honest about who called and silent about who decided to call, and "who decided" is the only distinction the console needs.

**The lag chart started empty.** `make eval-reset` deleted the recorded consumer-lag window, so the 15-minute chart held nought to two points while the fault it exists to show was climbing 0 → 10 → 30. Two samples drew that climb.

Different symptoms, one shape: the lab's own housekeeping erasing or impersonating the record the console is built from.

## Decision

### 1. `tools/call` accepts `_lab_probe`, in `params` and never in `arguments`

A caller may add a `_lab_probe` field beside `name` and `arguments`, carrying a short reason string. When it is honoured, the audit row for that call is written as **`lab.probe`** instead of `agent.tool_invoked`.

The placement is the load-bearing part. Inside `arguments` the field would be parsed by the tool's own input model — refused outright by the `extra="forbid"` most of them carry, and worse, present on the wire in `tools/list` for every tool that accepted it, which is the contract the commander pins and the surface the agent's planner reads. Beside `arguments` it reaches the envelope and stops there: **`tools/list` is byte-identical to v0.6.16** — same tools, same names, same descriptions, same input and output schemas, same `required_scope` and `is_idempotent` — so there is nothing to rebless, and no prompt can carry the field because nothing advertises it.

`lab.probe` joins `lab.` rather than `chaos.`, for the reason [ADR 0012](0012-the-lab-is-invisible-to-the-agent.md)'s 2026-09-20 amendment already gave for the boundary row: to the `/demo` console the newest `chaos.*` row **is** the fault, and a read is not a fault. It is withheld from the agent by the prefix rule that already exists — one prefix, one condition, no second rule — which is also the point. A read the agent did not make must not come back to it through `list_audit_events` as its own; otherwise the label would move the confusion one layer down instead of removing it.

### 2. The label is honoured only against `X-Lab-Principal`, and refused loudly otherwise

The field alone would let the agent lift its own reads out of the ledger by adding one key. The audit log would become something the subject under test writes, which is a worse defect than the one being fixed. So a labelled call must also carry:

```
X-Lab-Principal: Bearer <token>
```

and that token must verify to a service-account principal which is either

- holding **`chaos:invoke`** — the evaluator's account, identified by what it holds, which is the durable fact; or
- the **read-only smoke account**, matched by name (`lab_probe_smoke_account_name`, default `incident-commander-smoke`) **and** re-checked to hold no write scope. It has to be matched by name because it holds the agent account's scopes exactly — read-only is the point of it — so nothing else tells the two apart. The name is the weaker fact, hence the second check: an account that took this name and grew `actions:execute` is not the read-only smoke account.

and which belongs to **the caller's own tenant**, so a second tenant holding a copy of the credential cannot label rows in this one (the WO-R2-18 shape).

Anything else is a **refusal**: JSON-RPC invalid params, the call does not run, and the attempt is audited in the agent's own stream with the reason. Never a silent ignore — that would leave the row saying `agent.tool_invoked` while the caller believed it was labelled, which is exactly the mislabel this ADR exists to remove, now with a false receipt attached.

The credential is *verified, not adopted*: no tenant context is applied, no contextvar moves, the scope check for the tool itself still runs against the calling principal. All this second credential can do is label the row. The only trace it leaves is `last_used_at` on its own token.

**Gated on `CHAOS_ENABLED`** ([ADR 0008](0008-chaos-gating.md)), like everything else the lab owns. A production deployment has no lab, so it must have no path to relabelling an audit row, whatever credential arrives. Every stack an eval or a demo runs against has the flag on — that is where the chaos tools are registered — so this costs the evaluator nothing and closes the surface everywhere else.

### 3. The refusal names the field and the header, and no mechanism

One fixed sentence naming `_lab_probe`, `X-Lab-Principal` and the consequence, plus a closed `reason_code` (`not_available`, `credential_missing`, `credential_invalid`, `credential_not_authorised`, `reason_invalid`) on the wire and in the audit row.

It does **not** name the scope that would have authorised it. The refusal can reach the agent's own client — the agent is the one principal that could send the field by accident — and ADR 0012 rule 1 covers response bodies since its 2026-09-15 amendment. The rule in full lives here and in `docs/ARCHITECTURE.md`, which the commander's builders read and the agent cannot. The order's wording ("naming the rule") is met in the sense that matters operationally: the caller learns which half of the rule it failed, precisely enough to fix the request.

The reason string is bounded at **200 characters** and a longer one is **refused, not truncated** — the same rule ADR 0037 applied to its excerpts, for the same reason: it lands in `audit_logs.extra_data`, and a stored value the caller did not write is worse than a refusal it can read. (`audit_logs` has bitten this way before: an unbounded caller-supplied header used to delete the record of an action it was too wide for — R2-51.)

### 4. The label replaces `agent.tool_invoked`, and nothing else

If the row would have been `chaos.tool_invoked` / `chaos.tool_denied` ([ADR 0008](0008-chaos-gating.md)) or `agent.run_reported` ([ADR 0035](0035-the-agent-reports-its-run-and-cannot-read-it-back.md)), the action does not move and the reason rides along in `extra_data`. Those two streams already say the lab or the reporter made the call, and the `chaos.` one in particular is what the console reads as the fault — relabelling it would take a fact away rather than add one. The field is not ignored on those paths: the claim is recorded, and the row was already withheld from the agent.

### 5. The reset keeps the lag history

`scripts/reset_eval_state.py` stops deleting `kafka:consumer_lag:<group>:samples`. `lag_samples_cleared` stays in the summary as a permanent `0`.

The window is history. Its TTL is **longer** than the value key's on purpose (ADR 0037): the value must be fresh-or-absent because `check_backpressure` gates submissions on it, while history is most wanted at the moment the pass that writes it stopped. Deleting it on the reset boundary cancelled exactly that property — which is why the one paragraph of ADR 0037 that called the delete "load-bearing" is superseded here rather than left to be read as current.

The value key is untouched, as it always was: loop-owned under a 90 s TTL, so it is already fresh-or-absent without help and deleting it would only blind backpressure for a minute. The fear the delete addressed — a trend from the previous take read as this one's — is answered instead by the `lab.world_reset` boundary row: every sample carries its own `measured_at`, so a reader that must not cross the boundary compares against the row's timestamp. The world audit's `lag: 0` precondition reads the value, not the history, so it is unaffected.

The counter stays rather than being dropped because that summary dict *is* the boundary row's payload (WO-R3-327), and ADR 0036's rule is that every step reports a count: a key that disappears reads as a step that stopped being reported rather than one that stopped being needed. As a permanent 0 it is now a claim worth making — this reset preserved the window it used to delete.

## Consequences

**Positive.** The console can separate three actors it previously saw as one: the agent, the lab acting on the world (`chaos.`), and the lab acting through the agent's token (`lab.probe`). The agent's `agent.tool_invoked` stream becomes exactly what the agent did, which is what the `/demo` ledger and the phase strip are rebuilt from. And the lag chart opens on the history it has rather than on the two samples that survived the reset.

**Negative / costs.**

- **Two callers in the other repository must send both halves**, and until they do, F4 is unfixed — the platform half alone changes nothing about what the ledger shows. `evals/guards.py` sends the chaos credential; `evals/world_audit.py` sends the smoke one. Not this ADR's change to make, and named here so it is not mistaken for done.
- **The `/demo` console now receives `lab.probe` rows** and draws them, because it fetches `action_prefix=agent.,lab.,chaos.`. Excluding them behind a grey "evaluator probe" toggle is WO-R3-334. Until that lands the ledger shows the same reads under a different name — visibly the lab's, which is already an improvement on silently the agent's.
- **A second token verification** on a labelled call: one indexed lookup by token hash, on calls the lab makes, never on the agent's own path.
- **A name in configuration.** `lab_probe_smoke_account_name` is a platform setting mirroring a constant in the commander's bootstrap script. Mirrors drift; this one is overridable by env and is checked against the read-only claim, so the failure mode of a drifted name is a refused probe rather than a wrongly honoured one.
- **A reader of the lag window can now see across a reset boundary.** Deliberate, and the boundary row is how to tell.

## Alternatives considered

**Give the guards and the world audit their own principal.** The obvious answer, and it destroys what they measure. The guards assert what the *agent's* token can and cannot do; the world audit records what the *agent* would have seen. Under another principal both become assertions about a different account.

**Label by header alone, with no field.** Fewer moving parts, and it would label every call the credential accompanies — including a call the commander made for its own reasons and wanted in the agent's stream. The field is per-call because the decision is per-call, and it carries the reason, which is the part an operator reading the row actually wants.

**Honour the field on the agent's token alone (no second credential).** Rejected: see decision 2. The subject under test would be writing its own audit label.

**Ignore an unauthorised field instead of refusing.** Rejected, and this is the one worth stating twice: an ignore produces a mislabelled row *and* a caller that believes otherwise. A refusal is one line in a runner's log; a silent ignore is a ledger that is wrong in the direction of looking right.

**A new scope (`lab:label`) instead of the two-rule credential check.** A cleaner shape on paper, and it is a token migration on a live stack for a property `chaos:invoke` already expresses — plus it would still need the smoke account admitted some other way, since the whole point of that account is that it holds nothing.

**Withhold `lab.probe` under the `chaos.` prefix instead of `lab.`.** One prefix, one rule, nothing to add — and the `/demo` console reads the newest `chaos.*` row as the fault, so every evaluator read would have moved the incident clock. The same argument the boundary row settled.

**Keep clearing the lag window and have the console stitch a longer view client-side.** Rejected: that is what the console did before WO-R3-328, and it made the chart restart on every reload. The platform holds the window; the platform should not be deleting the only copy.

## Enforcement

- `backend/tests/unit/test_lab_probe.py` — the field is read from `params` and stays out of `arguments`; the field inside `arguments` is just an argument; the underscore spelling is the only spelling; the alias literal and the shared constant agree; each credential rule, each refusal reason; the refusal names no mechanism; **`tools/list` mentions none of this and its entry shape still carries the same six fields.**
- `backend/tests/unit/test_operator_audit.py` — the row's action and its two extra fields, that `lab.probe` is under the withheld prefix for a principal without `chaos:invoke`, and that it does not relabel a chaos row or a run report.
- `backend/tests/api/test_mcp_chaos_audit_visibility.py` — end to end on the MCP surface: an agent token alone cannot relabel (and the refused call leaves no `lab.probe` row), the evaluator and smoke credentials can, the agent's own credential in the header cannot, the field inside `arguments` labels nothing, an over-long reason is refused, a stack with no lab refuses every label, and the row is withheld from the agent — by prefix and by exact action — while the evaluator reads it.
- `backend/tests/api/test_audit_logs.py` — the operator sees the row with its payload, under the `lab.` filter the Audit tab already has.
- `backend/tests/unit/test_eval_reset.py` — the reset names neither consumer-lag key, the window-clearing helper is gone, and `lag_samples_cleared` reports the named constant.
- `backend/tests/integration/test_eval_reset_lag_history.py` — on a real Redis: the window survives every Redis-side step of the reset with its samples and timestamps intact, keeps its own long TTL, and still reads back after the value key it was measured beside has expired.
