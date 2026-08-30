# ADR 0025 — The alert severity vocabulary is `low | info | warning | critical`

**Status:** Accepted · **Date:** 2026-08-30 · **Owner:** Platform · **Decider:** user (WO-R2-45 → WO-R2-124)

## Context

`ALLOWED_SEVERITIES` was `{info, warning, critical}`, enforced in `AlertService.create_alert` before the row is created. There is no enum type, no CHECK constraint and no API-layer constraint — `Alert.severity` is a plain `String(16)`, so that service-layer check is the entire gate.

The incident commander's triage classifies noise with `_NOISE_SEVERITIES = {"info", "low", "unknown"}`. Two of those three branches could not be reached by any alert this platform is able to send, because the platform cannot produce `low` or `unknown` at all. Three eval scenarios sat on the gap, and for two of them the severity *is* the premise rather than setup: `noise_low_severity` and `noise_low_analytics` assert that a **low** alert is filtered at TRIAGE before INVESTIGATING spends budget. Rewriting those onto `info` would not adapt them — it would delete them and leave near-duplicates of the already-legal `info` scenarios.

The full analysis, including everything verified directly against this repo, is `docs/wave4-specs/R2-45-platform-widening.md` (filed by commander-3). This ADR records the decision it asked for.

## Decision

**Add `low`, and only `low`.**

```python
SEVERITY_LOW = "low"
ALLOWED_SEVERITIES = frozenset({SEVERITY_LOW, SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_CRITICAL})
```

No migration: the column is `String(16)` with no enum and no CHECK, so there is no DDL to change and existing rows are untouched. The value flows verbatim through `list_active_alerts`, `search_incidents` and the webhook body, and both MCP tools' severity filters document the widened set.

## Alternatives considered

### Narrow the commander instead (the serious counter-proposal)

Reduce `_NOISE_SEVERITIES` to `{"info"}`, retarget the two scenarios onto `info`, and delete a distinction the platform never supported. Genuinely cheaper: no platform change, no ADR here, and the commander's triage stops carrying branches production cannot reach.

Declined because it resolves the disagreement in the wrong direction. `low` is a band operators actually use — "worth recording, not worth waking anyone" — and the platform's inability to express it was an omission, not a considered position. Narrowing would have made the classifier honest about a vocabulary that was itself too narrow, and the two scenarios would have become duplicates of the `info` pair rather than remaining the only coverage of a distinct triage outcome. This was the user's call to make, and it went to widening.

### Also add `medium` and `high`

Declined. They map onto `warning` and `critical` with nothing left over. They appeared in 29 scenarios where the severity was incidental, and all 29 were rewritten commander-side onto the accepted bands. Adding them would widen the vocabulary to accommodate fixtures rather than reality, and would leave two ways to say each of two things.

### Also add `unknown`

Declined, and this one is a category error rather than a close call. `unknown` is the *receiver's* default for a payload it could not parse (`AlertPayload.severity: str = "unknown"`). Accepting it here would let a producer assert "I don't know how bad this is", which is a different and much larger product question: what a downstream consumer should do with an alert whose severity is explicitly absent. Nothing in this order requires answering it.

### Express "the field was absent"

Not possible under any widening, and deliberately not attempted. `Alert.severity` is `nullable=False` and the webhook body always carries `"severity"`, so no accepted value expresses absence. The scenario that tests this (`noise_missing_severity`) is a statement about the commander's defensive ingress, not about the platform, and its disposition belongs to the commander corpus.

## Consequences

* The commander's noise filter now has two of its three branches reachable from a real platform alert. `unknown` remains unreachable by design.
* `list_active_alerts` returns a value from a wider domain than before. That is live drift in an agent-facing tool's observed value domain, which the commander's drift check is expected to surface; the scenario un-marking happens at the wave-10 post-re-pin order and is not this change's business.
* No ordering or thresholding logic exists anywhere in the platform for severity — every consumer filters by equality — so a fourth value introduces no ranking question here. A consumer that later wants "at least warning" has to define the order explicitly, and should do so in its own ADR.
* The refusal path is unchanged and still the only gate: `medium`, `high`, `unknown` and everything else are rejected before the row is created, which `test_the_declined_values_are_still_refused` pins so that widening by one value cannot quietly read as widening in general.

## Verification

`backend/tests/unit/test_alerts_service.py` — the accepted set asserted as a whole rather than sampled; the four declined values parametrised as refusals; and an end-to-end test that a `low` alert is accepted, persisted, delivered *after* the commit, and arrives with `"severity": "low"` in a body covered by the `{timestamp}.{nonce}.{body}` signature (ADR-less but documented in ARCHITECTURE.md, WO-R2-70). A severity the service accepts but the emitter drops would not be a supported severity, because the commander classifies on what arrives.
