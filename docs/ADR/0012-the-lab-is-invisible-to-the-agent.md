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

> **Follow-up built (2026-08-09).** That registry-level test now exists:
> `backend/tests/unit/test_lab_invisibility.py` screens every tool whose
> `required_scope != chaos:invoke` — description, `required_scope`, plus
> serialized `inputSchema` and `outputSchema` — for a match on
> `chaos|eval|seed|fixture|harness|scenario`. It keys on `required_scope`
> rather than `is_chaos` so the screen still holds in a chaos-*enabled*
> environment, where the chaos tools are registered but remain exempt.
>
> **Inflection gap closed (WO-R2-62).** The stems were originally anchored
> with `\b` on both sides, so the screen matched only the exact singular:
> `fixture` was banned and `fixtures` was not, and the same held for
> `scenarios`, `evals`, `seeds`, `seeding` and `harnesses`. Those are the
> forms a description is *more* likely to use, so the gate was letting through
> most of what it was written to stop. The trailing boundary now tolerates a
> closed set of inflections (`-s`, `-es`, `-ed`, `-ing`). It stays a fixed set
> rather than an open `\w*` so that "evaluate" and "evaluation" — which this
> ADR calls legitimate — keep passing, and so that reading the pattern still
> tells you which words are banned.
>
> **Surface widened (WO-R2-32).** `tools/list` now also advertises
> `required_scope` and `is_idempotent`. `required_scope` was added to the
> screened surface: it is a closed 5-member enum today, but it is a string on
> the wire, so the screen covers it if that ever becomes free-form.
> `is_idempotent` is excluded deliberately rather than by oversight — a bool
> has no vocabulary to leak.
>
> It caught six leaks the one-tool regression test could not, all shipped in
> `v0.4.9`'s 26-tool surface: `list_active_alerts` ("chaos runs"),
> `list_audit_events` (the `chaos.` stream, named twice), `list_dlq_messages`
> (`remediation_hint` "set by triage / seed / chaos", inside `outputSchema`),
> `get_consumer_lag` ("7 eval-seed groups", "the eval seed script"),
> `get_deploy_history` ("the seed script annotates one row … for exactly this
> hypothesis-testing use case"), and `get_dag_state` ("Seed job" — the
> legitimate graph sense, reworded to "Root job" so the screen needs no
> allowlist). All six are reworded; the lab-provenance phrases are gone and no
> FRESHNESS or semantic content was touched.
>
> **The screen is stricter than prose.** Pydantic derives a JSON-Schema `title`
> from every field name, so `get_dag_state`'s `seed_id` output property
> serialized as `"title": "Seed Id"` — lab vocabulary on the wire that no
> author ever typed. Renaming the property would break the output contract, so
> the field carries an explicit `title="Root Job Id"` instead. If a future
> wording trips the screen, reword it or title it; do not widen the pattern.

> **Scope limit (recorded 2026-08-09): invisibility is per ENVIRONMENT, not per PRINCIPAL.**
> `tools/list` is dispatched without the caller's principal, so in a chaos-*enabled* environment any
> authenticated principal — including a read-only smoke token — can enumerate all 8 chaos tools with
> their schemas and `[chaos: …]` description prefixes, and a probing `tools/call` confirms existence
> via the scope-denial message. Invoking them still requires `chaos:invoke`, so this is an
> information leak inside the lab, not a path to firing chaos. Closing it (a scope-filtered
> `tools/list` plus not-found masking for chaos denials only) is deferred past the eval restart:
> it changes what every principal sees in the exact call that generates the commander's contract
> snapshot, and this campaign performs that rebless exactly once. Reasoning and implementation
> sketch in [ADR 0016](0016-defer-principal-scoped-tools-list.md).

## Alternatives considered

**Redact chaos keys at the serialization layer** rather than removing the fields. Rejected: it keeps a field whose value is always redacted, which is a worse contract than not having the field, and it invites the assumption that redaction is a general safety net when it would only ever cover the patterns someone remembered to list.

**Per-scenario tenant isolation** instead of an empty DLQ baseline (commander ADR 0010's option 3). The clean-room answer, and multi-tenancy could support it — but it multiplies seed time per scenario and complicates the service-account story (scope per tenant per scenario) to solve a problem the empty baseline already solves for the only shared surface that has actually bitten. Revisit if traces or deploy history start contaminating scenarios the same way.

**Keep the fixture pool, document it as expected furniture.** Rejected: it asks the agent to learn which populated DLQ is real and which is scenery. That is not a skill worth training, and it is not a distinction an on-call SRE would ever have to make.

## Amendment (2026-08-09) — reset disposal vs audit ground truth: `resource_id` is the durable join key

Rule 2's disposal decision ("declared scaffolding is **deleted** on reset, not cancelled") is unchanged and stands. This amendment records the consequence it has for the audit log, which was previously implicit, plus the contract that consequence forces on anything grading against audit rows.

**What the DELETEs do to `audit_logs`.** `reset_eval_state.py` hard-DELETEs jobs (`_delete_seeded_dlq_fixtures`) and chaos-owner users together with their jobs (`_delete_chaos_owner_users`). `audit_logs.job_id` and `audit_logs.user_id` are FKs declared `ON DELETE SET NULL` (`app/models/audit.py`), so **every audit row referencing deleted scaffolding has those two columns nulled on every reset.** That is by design and stays: the alternative — `ON DELETE CASCADE` — would destroy audit rows outright, and audit is ground truth (commander invariant 6). `job_triages` rows do CASCADE-delete with their job (`app/models/triage.py`); recorded here as a decision rather than an accident, on the same reasoning as the disposal rule itself — a triage row about a seeded fixture is scaffolding's scaffolding, never a real user's history.

**What the reset never does.** It does not write, update or delete a single `audit_logs` row. No statement in the script names the table; `backend/tests/unit/test_eval_reset.py::test_reset_sql_never_names_audit_logs` parses the module's `text()` literals and fails if one ever does. The DB layer backs this up independently for the runtime role: migration `b8e4a1c92f35` revokes UPDATE/DELETE on `audit_logs` from `incident_app` ([ADR 0015](0015-force-rls-and-nonowner-app-role.md)), while the FK's SET NULL still fires because referential actions run with the referencing table owner's privileges.

**The contract.** The durable identity of a deleted job or user, as seen from the audit log, is:

- **`audit_logs.resource_id`** — a `String(255)`, written as `str(job.id)` by every Tier-1 audit writer (`replay_dlq_by_ids.py`, `replay_dlq_by_category.py`, `mark_dlq_permanent.py`, and the seed script). It is not a foreign key, so nothing nulls it.
- **`audit_logs.extra_data`** — the JSON side-car carrying the action's before/after detail.

Both survive the reset byte-identical. **Any audit-based grading, trajectory analysis or forensic query must join on `resource_id`, never on `job_id` / `user_id`.** A grader joining on `job_id` silently undercounts — it does not error, it just stops seeing the rows whose subject the reset disposed of, and the undercount grows with every campaign.

This is the same shape the machine-principal identity already uses: `principal_id` is a plain unconstrained UUID precisely so a deleted principal neither cascades nor blocks audit reads ([ADR 0007](0007-machine-principal-scope-model.md)). The rejected fix here was to give `job_id` that treatment too — dropping the FK "to preserve the value". Rejected: it rewrites migration history and the read paths of a convenience column to duplicate an identity `resource_id` already carries.

Also rejected, explicitly: replacing the DELETEs with status flips or a shadow-table archive so the FKs survive. That contradicts rule 2's accepted disposal decision, and an accepted ADR is not silently contradicted by a patch. If the disposal rule should change, that is a revision of this ADR, argued on its own merits.

**Enforcement.** `backend/tests/integration/test_eval_reset_postgres.py::test_delete_chaos_owner_users_nulls_audit_fks_but_keeps_resource_id` asserts all three halves on real Postgres (SQLite's FK enforcement is PRAGMA-dependent, so the unit harness cannot): the audit row survives, `job_id` / `user_id` are NULL, and `resource_id` still equals the deleted job's UUID string. Asserting the NULLs — rather than ignoring them — is deliberate: a future migration flipping either FK to CASCADE would delete the audit row and fail that test loudly.

**Cross-repo follow-up (not actioned here).** The commander side must be checked against this contract: any grader or trajectory analysis joining audit rows on `job_id` needs to move to `resource_id`. That is a commander PR; this ADR only makes the platform-side guarantee explicit enough to hold it to.

## Amendment (2026-09-15) — rule 1 covers RESPONSES too, and it takes two principals to mean it

Rule 1 was written about the wire surface `tools/list` serializes: descriptions and schemas. Everything it says stands. This amendment records that the rule was incomplete in a way nothing on the platform could have caught, and what closes it (WO-R3-187, owner decision O-4).

**What leaked.** `list_audit_events` returned the `chaos.tool_invoked` / `chaos.tool_denied` stream to any `incidents:read` principal, with `extra_data` naming the hook and the arguments it was called with, and `get_trace` returned the same rows for the trace a chaos invocation ran under. The tool's own description had been cleaned of chaos vocabulary a release earlier; the descriptions were invisible and the **data** was not. An agent investigating a seeded fault could ask the platform who injected it.

**The rule, restated.** A non-chaos tool may not name the lab in its description, its schemas, **or the body of what it returns**. The first two are screened by `backend/tests/unit/test_lab_invisibility.py` off the registry; the third is screened by `backend/tests/api/test_read_tools_never_name_the_lab.py`, which calls every registered read tool on a chaos-seeded world as a principal without `chaos:invoke` and asserts the serialized response contains no `chaos` substring. That test is parameterised off the registry and fails by name on a read tool it has no call for, so a tool added later cannot quietly skip the screen.

**Why two principals.** The withholding is keyed on `chaos:invoke` (`app/services/operator_audit.py::hidden_audit_action_prefixes`): whoever may fire the lab may read the lab. That predicate is inert while one token holds every scope, and the live `incident-commander` account held `chaos:invoke` precisely so live remediation runs could seed themselves. So the filter ships with a token split: `scripts/seed_incident_commander.py` now mints two — `incident-commander` (reads plus `actions:execute`, and the script removes `chaos:invoke` if it finds it) and `incident-commander-chaos` (reads plus `chaos:invoke`) — printed as `PLATFORM_TOKEN` and `PLATFORM_CHAOS_TOKEN`. The evaluator still sees every row it grades against; the agent sees none of them.

**Human operators are unaffected.** The admin Audit tab reads the REST audit API, not the MCP tool. Invisibility is a property of the machine principal under test, never of the audit trail: the rows are ground truth for grading, and an operator is supposed to see everything that happened.

**Withholding is not refusing.** `action_prefix='chaos.'` returns an empty page with `total: 0`, and so does an exact `action='chaos.tool_invoked'`. An error would confirm the stream exists, which is the fact being withheld; a `total` counting hidden rows would disclose them as a number. Both exclusions therefore run in SQL (`AuditRepository.list_logs(exclude_action_prefixes=...)`), where `total` is counted under the same `WHERE`.

**What this amendment does NOT do.** It does not reopen [ADR 0016](0016-defer-principal-scoped-tools-list.md). `tools/list` is still not principal-scoped, chaos tools still advertise their `[chaos: <blast_radius>]` prefix to any authenticated principal, and the commander still identifies chaos hooks in its contract snapshot by that prefix — masking them would empty every scenario's chaos validation. Descriptions are out of scope here by decision, not by oversight (divergence report row G4).

**Known residual leaks, response-side.** Two channels still carry the word and are listed with their reasons in `backend/tests/api/test_read_tools_never_name_the_lab.py`, each with a tripwire test that fails when its channel changes: `bad_deploy`'s alert `source` / `title` (blocked on owner decision O-8, because `scripts/reset_eval_state.py` resolves those alerts by `source LIKE 'chaos:%'` and a rename without the predicate re-creates WO-R2-131), and the `(chaos poison_message on topic …)` suffix on the DLQ error text `poison_message` writes (a coupled commander-side fixture rebless). Both are named here so the next reader can tell a deferral from a miss.


## Amendment (2026-09-16) — poison-message response text (WO-R3-251)

The poison-message suffix now names the topic and the producer correction, without naming the lab. The response sweep includes its error text and payload markers and screens every read tool without a poison-text exception. The internal `chaos_fixture` marker remains for provenance: current read tools do not return job payloads. The bad-deploy alert and latent owner-email exceptions remain deferred. This response-text delta needs the commander fixture/dossier update after the platform release; story keys and tool schemas are unchanged.

## Amendment (2026-09-20) — the withheld set is `chaos.` **and** `lab.` (WO-R3-327)

Nothing above changes. This amendment records a second audit prefix joining the first, and why it is a second prefix rather than a member of the first.

**What is new.** The environment reset now appends one audit row per run, `lab.world_reset`, whose `extra_data` is that reset's own summary of counters. It exists for a console problem, not a leak: `audit_logs` is append-only, so after `make eval-reset` the newest `chaos.*` row was still the *previous* take's fault, and the `/demo` page opened a freshly wiped world at `agent remediating` with a clock counting from an incident that no longer existed. The page cannot infer the boundary — it is not in the audit log, the Redis keys are gone, and `agent_runs` says only that a run ended — so the reset states it, on the platform's clock, in the same append-only place as the rows it bounds.

**Why it is withheld, and more firmly than a hook name.** `hidden_audit_action_prefixes` now excludes `lab.` wherever it excludes `chaos.`, under the identical condition: shown only to a principal holding `chaos:invoke`. Two reasons, and the second is the stronger one.

- The row's very existence is a fact about the apparatus. A principal that can see its world being wound back and re-seeded on a schedule knows it is in a lab, without reading a single field.
- **Its payload is the mechanism list.** `chaos_keys_cleared`, `seeded_dlq_deleted`, `hot_set_reseeded`, `dag_pauses_cleared`, `breakers_reset` — the counters name what the lab sweeps, which is a broader disclosure than any one `chaos.tool_invoked` row makes. Note the 2026-08-09 registry screen would not have caught this: it screens tool descriptions and schemas, and this is a *response body* — the gap the 2026-09-15 amendment named. The withholding is what closes it, not the screen.

**Why a second prefix and not `chaos.world_reset`.** This is the one decision in the change with a real alternative. The `chaos.` stream would have carried it for free: one prefix, one rule, nothing to add. It was rejected because of what reads that prefix. To the `/demo` console, *the newest `chaos.*` row is the fault* — that is how the strip knows a fault was injected and what the clock counts from. A boundary filed under `chaos.` would therefore be read as a fault, which is precisely the reading it exists to remove: the bug would have changed shape rather than gone away, and every consumer of that stream would have needed an exception for one action name. `chaos.` is the lab acting *on* the world; `lab.` is the lab acting on the timeline. Different prefixes, one condition.

**What it is not.** It is not a tool, so nothing in `tools/list` moves: no name, description, schema, scope or `is_idempotent` flag, and there is no contract delta to rebless (audit action names are not schema). It is not a chaos hook, so it carries no `BlastRadius` member and writes no Redis key. It is not idempotent and must not be — one row per reset is the whole point, and the console reads the newest.

**The principal is the evaluator's service account** (`incident-commander-chaos`, `SA_CHAOS_NAME`), looked up in the seed tenant so a second tenant holding a copy cannot raise from the tail of the reset (the WO-R2-18 shape). It is the principal that fires every `chaos.*` row in the same timeline, so an operator sees one actor for the whole lab; and it is the only principal the rule above lets read this row back, so the row's author and its one machine reader are the same identity. On a stack where that account has not been seeded the row is still written, with a null `principal_id` — a boundary nobody signed is worth more than no boundary. Unlike `record_tool_invocation`, the write is **not** savepoint-wrapped and does raise: there is no response to protect, and a reset whose boundary was never recorded leaves the console reading the previous take as current.

**Human operators are unaffected**, as in every amendment here. `GET /audit/logs` applies no exclusions, the admin Audit tab gains a `lab.` stream filter, and the `/demo` timeline draws the row as a grey divider with the rows of the closed take below it.

**Enforcement.** `backend/tests/unit/test_operator_audit.py` pins the prefix, the two rules' independence and the writer's shape; `backend/tests/api/test_mcp_chaos_audit_visibility.py` proves the evaluator reads the row and the agent gets an empty page for it by prefix and by exact action, with none of the counter names surviving in the response; `backend/tests/api/test_audit_logs.py` proves the operator sees it with its payload; `backend/tests/unit/test_eval_reset.py` proves the reset appends exactly one, that it is outside the `chaos.` stream, and that the reset still writes no other audit row and no raw SQL naming the table.

## Amendment (2026-09-20) — a probe the lab makes on the AGENT's token is labelled by the lab (WO-R3-333)

Everything above stands. This amendment records the case the rules above did not cover: not the lab hiding itself from the agent, but the lab **wearing the agent's identity** — and the row that leaves behind.

**What happened.** Two evaluator callers use the agent's own token deliberately, because the token is the subject of the assertion: `evals/guards.py` proves the agent's principal cannot execute a Tier-1 action and cannot fire a hook, and `evals/world_audit.py` reads the world exactly as the agent would read it. Both wrote `agent.tool_invoked`, and the demo's third live take showed seven of those rows in the `/demo` action ledger after the reset boundary, indistinguishable from the agent's own investigation. The rows were honest about who *called* and silent about who *decided to call*, which is the only distinction the console needs.

**The rule.** `tools/call` accepts an optional `_lab_probe` reason string **in `params`, beside `arguments` and never inside it**; the audit row for that call is then `lab.probe` rather than `agent.tool_invoked`. The label is honoured only when the request also carries `X-Lab-Principal: Bearer <token>` verifying to a principal holding `chaos:invoke` or to the read-only smoke account, in the caller's own tenant, on a `CHAOS_ENABLED` stack. Otherwise the call is **refused** — JSON-RPC invalid params, nothing runs — because a silent ignore leaves the row mislabelled and the caller believing otherwise. Full reasoning, the rejected alternatives and the enforcement list are in [ADR 0038](0038-a-probe-by-the-lab-is-labelled-by-the-lab.md).

**Why this belongs to this ADR.** Rule 1 says the agent's observable surface contains only the system under test. `lab.probe` extends that in the direction rule 1 never faced: a call the lab made under the agent's token is apparatus, so it is withheld from the agent — by the `lab.` prefix and the one `chaos:invoke` condition the 2026-09-20 amendment above already established, with no second rule added. The agent reading back a read it never made would be the apparatus leaking into the experiment as surely as a hook name in a response.

**Two consequences worth stating in this file.** The field is outside `arguments` *because* of this ADR's registry screen: inside, it would appear in `tools/list` for every tool that accepted it, and the agent's planner reads that surface. `tools/list` is byte-identical — no name, description, schema, scope or `is_idempotent` flag moves — so there is no contract delta to rebless. And the **refusal message names no lab vocabulary**: it can reach the agent's own client, which is the one principal that might send the field by accident, so it names the field, the header and a neutral reason code, never the scope that would have authorised it. The rule lives in ADR 0038 and in `docs/ARCHITECTURE.md`, where the agent cannot read it.

**Human operators are unaffected**, as in every amendment here. `GET /api/v1/audit/logs` applies no exclusions, and the Audit tab's existing `lab.` stream filter finds the new action with no new option.
