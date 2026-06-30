# ADR 0005 — LLM-driven features fail open

**Status:** Accepted (Phase 10 PRs #34, #39, #40, #41) · **Date:** 2026 Q2 · **Owner:** Platform

## Context

Phase 10 added four LLM-driven features, all using the Anthropic API:

1. **DLQ triage** — Claude classifies the root cause of a dead-lettered job and writes a summary + suggested fix
2. **LLM-guided retry policy** — Claude decides whether a failing job should retry or be dead-lettered immediately
3. **Natural-language admin queries** — Claude translates plain English into a constrained job filter
4. **Periodic incident summaries** — Claude writes a daily digest of failures for each tenant

Each feature talks to a third-party API over the public internet, costs real money per call, and has variable latency (sub-second on cache hits; multi-second on a cold call with adaptive thinking). The Anthropic API can also be temporarily unavailable — regional outages, rate limits, our own missing API key in a fresh env.

The question for every one of these features: **what happens when the LLM call fails?** Three options:

1. **Fail closed** — return an error to the caller, halt the operation
2. **Fail open** — degrade to a non-LLM fallback (retry, deterministic policy, dropped feature) and continue
3. **Block and retry** — keep trying the LLM until it succeeds

## Decision

**Every LLM feature fails open.** The platform must remain usable when Claude is unavailable. Each feature has a deterministic non-LLM fallback that's exercised when:

- The feature flag is off (`LLM_TRIAGE_ENABLED=False`, etc.) — the dominant case in fresh environments
- `ANTHROPIC_API_KEY` is unset
- The API call times out (configurable per feature; defaults to 10s)
- The API returns an error (rate limit, 5xx, network blip)
- The model's response fails Pydantic schema validation
- The model refuses to respond

The fallback per feature:

| Feature | Fallback |
|---|---|
| DLQ triage | No triage row written; admin sees the job in DLQ with raw error_message and uses their own judgment (the pre-Phase-10 experience). |
| LLM-guided retry policy | Deterministic exponential backoff (`retry_backoff_base ** retry_count`). Same behavior as if the feature didn't exist. |
| Natural-language admin queries | API returns 503 with `error_code: nl_query_unavailable`. UI shows "try rephrasing or check the feature flag." Structured filters in the same tab still work. |
| Periodic incident summaries | Digest loop logs the failure, continues with the next tenant. The persistent record isn't written; admin sees the previous digest until the next successful run. |

Notably the retry-policy feature is the one most worth zooming in on: the LLM call happens *inside* a worker's failure path. A failing LLM cannot block job retries, or the platform becomes unavailable whenever Anthropic does. The code path is explicit:

```python
if eligible_for_llm_consult:
    try:
        decision = await retry_policy.decide_retry(...)
        # apply decision
    except Exception as policy_exc:
        logger.warning("retry policy fell back to deterministic", ...)
        # delay was already set to the deterministic exponential backoff above
```

## Alternatives considered

### Fail closed (require the LLM)

Tempting for the LLM-guided retry policy: "if Claude says dead-letter, we want that to be authoritative, not silently bypassed."

**Why not:** the worker's job is to keep moving jobs through the pipeline. A 30-minute Anthropic outage cannot become a 30-minute pipeline outage. The deterministic backoff has been the system's correctness boundary since Phase 2; the LLM is a refinement on top.

### Block and retry

Keep trying the LLM until it succeeds, exponential backoff on the call.

**Why not:** for the retry-policy feature this blocks the worker indefinitely. For the digest feature it wedges the loop. The cost in latency is unbounded.

### Per-feature mixed policy

E.g. retry policy fails open, triage fails closed.

**Why not:** consistency makes the platform predictable. Operators can reason about "what happens when the LLM is down" without needing a per-feature lookup.

## Consequences

### Positive

- **Anthropic outages don't cascade.** A regional API outage affects the *quality* of certain features (deterministic backoff instead of LLM-tuned; no triage rows; no NL search) but the platform keeps working.
- **Tests pass without an API key.** Every LLM service has a "disabled by default" path that's exercised in CI; no test requires `ANTHROPIC_API_KEY`.
- **Cost cap by feature flag.** Operators can turn off any LLM feature instantly without code changes.
- **Recovery is automatic.** When the API recovers, the next call succeeds — there's no manual reset.

### Negative

- **Silent quality degradation.** When the LLM is down, features quietly run worse instead of erroring loudly. The retry policy reverting to deterministic backoff doesn't page anyone; the digest tab going stale doesn't either. Mitigated by structured logging — every fallback writes a `WARNING` with the underlying exception — but the operator has to be looking.
- **The `dead_lettered_by: llm_retry_policy` audit trail is incomplete.** When the LLM fails, the audit log doesn't say "we tried the LLM and it timed out, so we're going deterministic." It just shows the deterministic path. Acceptable but worth knowing.
- **Cache-warming asymmetry.** The system prompt's cache TTL is 5 minutes. A few minutes of API failure cools the cache; the next successful call pays the cache-creation cost. Visible in `usage.cache_creation_input_tokens > 0` after recovery.

## Operational impact

Every LLM service exposes the same envelope on its persisted rows:

- `model_used` — string, e.g. `claude-opus-4-7`
- `usage` — dict with `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`

This is the cost-telemetry surface. The admin Digests tab and the triage card both surface it, so operators can see cache-hit rates over time and catch creep before the bill arrives.

## Pointers

- `backend/app/services/triage.py` — DLQ triage
- `backend/app/services/retry_policy.py` — retry policy
- `backend/app/services/nl_query.py` — NL queries
- `backend/app/services/incident_digest.py` — digests
- `backend/app/workers/dispatcher.py` — the worker's `try/except` around the policy call (the canonical example of fail-open)
