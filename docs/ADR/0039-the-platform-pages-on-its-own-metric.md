# ADR 0039 — The platform pages on its own metric, and an episode raises once

**Status:** Accepted · **Date:** 2026-09-20 · **Owner:** Platform · Owner decisions O-35 and O-36 · Amends the sampling claim in [ADR 0037](0037-a-run-record-carries-the-run.md) · Builds on [ADR 0012](0012-the-lab-is-invisible-to-the-agent.md), [ADR 0015](0015-force-rls-and-nonowner-app-role.md), [ADR 0025](0025-alert-severity-vocabulary.md), [ADR 0026](0026-strict-tenant-isolation-and-declared-platform-scope.md), [ADR 0027](0027-control-loop-pause-closed-enum.md), [ADR 0028](0028-outbox-relay-heartbeat-and-delivery-reading.md) and [ADR 0036](0036-the-reset-closes-a-breaker-and-a-registry-it-cannot-restart-honours-it.md)

## Context

The live demo's fourth take was green and still not watchable, and the audit stream said why. Two of the reasons are this platform's.

**The platform never paged anyone.** The alert the responder triaged was written by the scenario file that ran it (`evals/scenarios/*.yaml`, an `alert:` block with a `fingerprint`), and the platform's own alert stream read the same three seeded fixtures before, during and after the fault — `list_active_alerts` returned `total: 3` throughout. The demo's sentence is "jobs pile up, the platform pages, the agent responds"; the middle clause was a fixture. Since WO-R2-29 the only non-chaos producer of an `Alert` row has been the SLO fast-burn evaluator, which answers a question about a 24-hour error budget and says nothing about the two conditions an operator watches minute to minute.

**The clock was too slow to watch.** `_METRICS_LOOP_INTERVAL = 60.0` was a module constant, so the lag number an operator is staring at moved once a minute, the 15-minute chart gained one point a minute, and the demo's step 3 waited on a value that could not appear sooner. Nobody watching a demo waits a minute for a number to move (O-35).

Both were decided by the owner on 2026-09-20 (O-35, O-36) with the rule that the canned and eval scenarios keep their YAML alerts: this changes what the *platform* does, not what a graded scenario asserts.

## Decision

### 1. The metrics pass interval is a setting, and two numbers are derived from it

`metrics_loop_interval_seconds` (env `METRICS_LOOP_INTERVAL_SECONDS`, default **60.0**), read every pass, clamped to a 1 s floor in one function — `core/consumer_lag.metrics_interval_seconds` — which `control_loop_pause.tick_interval_seconds` also reads, so "resumes in N ticks" is the wait the loop will really take. `METRICS` therefore leaves the mirrored-interval table and joins `digest` and `slo_evaluation` as settings-derived.

Two numbers that used to be literals are now derived from it, because a faster clock made both of them wrong in different directions:

- **The lag value key's TTL** was a fixed 90 s, written beside the comment "must exceed metrics loop interval (60s)". At 60 s that is 1.5 passes; at 5 s it is eighteen, so a number measured a minute and a half ago would still be readable as current on the one stack anybody watches — and the value key's whole contract is that `check_backpressure` and `get_consumer_lag` read a fresh measurement or nothing ([ADR 0037](0037-a-run-record-carries-the-run.md) states the asymmetry with the window beside it). It is now **three passes** (floor 15 s): **180 s at the 60 s default, 15 s at the demo stack's 5 s.** Three rather than 1.5 deliberately — one lost pass must not blank the reading on a healthy stack, because an absent value opens the admission gate.

- **The lag window's length** was a count, `LAG_SAMPLES_KEEP = 15`, justified as "the window over the interval". The moment the interval became a setting that count silently became 75 seconds of history at a 5 s tick — a fifteen-minute axis holding a minute of data. `LAG_SAMPLES_WINDOW_SECONDS = 900` stays and the ring is **pruned by time**, so the span is the promise and the density follows the clock: 15 samples at 60 s, **180 at 5 s**. `LAG_SAMPLES_MAX_ENTRIES = 240` remains as an absolute guard against a pathological interval or a key written by something else, and it does not bind at any interval above 3.75 s. One consequence, stated rather than discovered later: a `get_consumer_lag` response on a 5 s stack carries up to 180 samples instead of 15.

The reader bounds what it returns by the absolute cap and **not** by a count derived from its own settings, because the reader runs in the API and MCP processes (ADR 0006) and a reader trimming the window to *its* interval would hide samples the writer kept whenever the two disagreed.

**The two surfaces return different amounts of that ring, and the asymmetry is the decision.** `GET /admin/consumer-lag` serves the whole time-pruned window: an operator's chart wants every point in the fifteen minutes and is drawn once. `get_consumer_lag` returns `LAG_SAMPLES_AGENT_CAP = 15` — the newest fifteen, which is the count and the order it returned before the clock became a setting — because the agent pays for each sample in its context on *every* read, and a reading whose SIZE changed with a deployment's tick would make one stack's investigation quietly more expensive than another's for no new information. Fifteen samples answer "climbing, draining or flat" at any interval, and `age_seconds` plus the gaps between them still say how often this deployment measures. The cap is stated in the description, because a cap the caller cannot see is worse than an error (CLAUDE.md: never promise completeness you cap).

### 2. Two rules the platform evaluates on its own clock

`services/alert_rules.evaluate_alert_rules` runs in the metrics pass, on the same tick, right after the measurement it reads — a rule evaluating between two measurements can only re-read the number it already saw. Both are gated by `alert_rules_enabled` (default **on**; a deployment with a real pager in front of it turns them off rather than deleting them).

| Rule | Fires when | `source` | Payload subject |
|---|---|---|---|
| `consumer_stalled` | the latest **measured** lag sample for a group is ≥ `consumer_lag_alert_threshold` (default 20) | `kafka:consumer_lag` | `consumer_group` + `group` |
| `dlq_depth_warning` | a tenant's dead-letter total is ≥ `dlq_depth_alert_threshold` (default 5) | `dlq:threshold` | `remediation_hint` **or** `dlq_scope` |

Both raise `severity=critical` from the vocabulary [ADR 0025](0025-alert-severity-vocabulary.md) fixed, through `AlertService.create_alert`, so the existing signed webhook fires when a URL is configured and `list_active_alerts` shows them when it is not. The lag rule pins its alert to the platform tenant, like a fast-burn alert: Kafka lag is not a property of a customer. The DLQ rule is per tenant, because a dead-letter backlog really is one tenant's.

The DLQ threshold's default is the seeded baseline of four dead-letter fixtures **plus one**, so a world nobody has hurt stays quiet and the first row that is not part of the baseline pages.

### 3. An episode raises once, and a second episode raises again

`dedup_key = rule:<fingerprint>:<subject>:<ordinal>`, where the ordinal counts the episodes that series has already had. The unique constraint on `(tenant_id, dedup_key)` is the de-duplication, exactly as it is for a fast burn: `worker_loop` runs in every replica, both evaluate the same tick, both find no open episode, both compute the same ordinal, and Postgres lets one commit. A sustained breach therefore pages once however many ticks it spans, and a new episode after a recovery gets a new key instead of being suppressed forever by the old one.

An episode ends when a reading crosses back: the first lag sample below the threshold, or a depth below it. Resolution stamps `resolved_at` — **resolved, never deleted**, because the alert id is quoted in run output and `list_active_alerts` keys on the null.

**Rejected: a Redis episode marker.** The obvious shape is `SET NX` on `alert:episode:<rule>:<subject>` holding an episode id. It works and it is one more piece of state outside the namespaces `make eval-reset` sweeps — the class of defect [ADR 0036](0036-the-reset-closes-a-breaker-and-a-registry-it-cannot-restart-honours-it.md) exists to close — and the failure mode of forgetting it is that the *next* take never pages, silently. Counting rows needs nothing swept.

### 4. `alert.raised` and `alert.resolved`, and the agent may read them

One audit row per transition, `resource_type: alert`, `resource_id` the alert id, carrying the fingerprint, the subject, the source, the severity and the one-sentence summary — enough for the console to draw a "platform paged" station from the audit stream alone. `principal_type` is `service_account` with a **null** `principal_id`: no human and no account did this, the platform's own loop did, and [ADR 0007](0007-machine-principal-scope-model.md) made that id nullable precisely so an actor without one still gets a row.

**These rows are withheld from nobody.** [ADR 0012](0012-the-lab-is-invisible-to-the-agent.md) hides the lab — a fault going in (`chaos.`), the reset's apparatus and a probe the lab took wearing the agent's token (`lab.`) — and the inverse rule hides a responder's own report stream from its writer. An alert is the opposite kind of row: it is the thing the agent was *paged with*, raised by a documented rule over a reading the agent can take itself, and nothing in it names a mechanism. Withholding it would hide the page from the responder answering it.

The write is not savepoint-wrapped and does not swallow. It shares the alert's transaction, so a transition the audit stream cannot carry does not become an alert — `record_world_reset`'s contract rather than `record_tool_invocation`'s, for the same reason: there is no response to protect, and a page the console cannot see is worse than a page that arrives one tick later.

### 5. The reset closes an episode the way a recovery does

`reset_eval_state` gains `_resolve_rule_alerts`, before the existing organic sweep, counted as `rule_alerts_resolved`. The sweep beside it needed **no predicate change** — it spares five `stable()` ids and resolves everything else, which was checked against its WHERE clause — but it writes no audit row, so closing an episode with it would leave an `alert.raised` with no `alert.resolved` after it and a console timeline holding a page that never ended. So the boundary goes through the rule's own resolution path, and the sweep stays the backstop it has been. The world audit's "active alerts: 3" is true after a reset again.

## Consequences

- **One contract delta.** `get_consumer_lag`'s description had "every ~60s", "90s TTL" and "up to a minute stale" in it — statements that were about to be false on the only stack anybody watches. The description now says the interval is **deployment-configured**, points at `age_seconds` and the gaps between `recent_samples`, and states the fifteen-sample cap on what it returns. `tools/list` moves: 40 tools before and after, no name, scope, field, `$defs` entry or `required` list changes, and exactly one entry differs — `get_consumer_lag`'s `description` (3,444 → 3,728 chars) and the `source` / `age_seconds` / `recent_samples` field descriptions in its `outputSchema`. Hash `31183161…` → `21a1c228…`. The commander re-pins and reblesses; the *reason* it is not avoidable is CLAUDE.md's own rule that a description which does not match the behaviour behind it is a functional defect that fails silently with a confident-looking answer.
- **The description must not interpolate the setting.** It would make `tools/list` deployment-dependent, so the snapshot the commander pins would differ between the demo stack and CI. This is why the wording points at a reading rather than at a number.
- **The default value-key TTL changes, 90 s → 180 s.** A stale lag is readable for three passes instead of 1.5 after the metrics loop stops. Deliberate: the loop stopping is the case where an absent value opens the admission gate. Anything that documented "90s TTL" as the platform's behaviour — including a comment in the commander's canned `cascading_redis_starves_backpressure` scenario — now describes a number that is derived.
- **A recorded constant is never a breach.** Seven consumer groups report a lag the seed wrote once (500 … 100,000) and nothing refreshes them, so a rule reading their *value* would page six times on a healthy world and never resolve. The rule reads the latest measured **sample**, which only the continuously-refreshed group has. Absence is not recovery either: a window that stopped being written leaves an open episode open, because unknown is not zero is this tool's own rule.
- **The DLQ alert says only what the platform can see.** The category it names is the one the rows above the baseline carry, taken newest-first by the same clock and order the agent's own `list_dlq_messages` page uses (`JobSort.DEAD_LETTERED_AT` — `COALESCE(completed_at, created_at) DESC`); when those rows carry no category it names `dlq_scope: unclassified` instead, and when they disagree it names neither. It does **not** consult the lab's `seeded_fixture` marker to decide which rows are "real", the way `services/slo.py` does for its fractions — a production rule reaching for a lab marker is a different decision, and this one does not need it.
- **A consequence for the demo, not for this repo:** `remediate_dlq_backlog_success.yaml`'s alert claims `remediation_hint: replay_safe`, and the row that pushes that world's depth over the threshold is the unclassified poisoned one. A platform-raised alert for that world is therefore the `unclassified` incident, not the `replay_safe` one. WO-R3-339 has to choose which world its DLQ take runs; the platform cannot honestly raise the other alert.
- **No schema migration, no new table, no new column, no new key, no new scope, no new refusal code.** The episode identity lives in a column that already exists under a constraint that already exists.
- **Not built, and recorded rather than solved:** nothing ages an episode out. A group whose window stops being written keeps its alert open until a sample reads below the threshold, which is the honest reading of an unknown but means an abandoned stack shows a page forever. `make eval-reset` closes them; a production deployment would want a staleness rule, and it is a decision rather than an oversight.
