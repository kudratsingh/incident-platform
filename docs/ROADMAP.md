# Roadmap — open extensions

Ideas for extending the platform, organized by what skill they showcase and what they'd cost to ship. Pick any of these as a next PR.

Sizes are rough:

- **S** — one focused PR, half a day to a day
- **M** — one PR, 1-2 days
- **L** — sequence of 2-3 PRs, a week
- **XL** — phase-level, multi-week

Roadmap items are NOT promises — they're a thinking inventory.

---

## Already on the formal plan (CLAUDE.md phases)

These are sized at "phase" level in the milestone plan. Not repeated here, but referenced for context:

- **Phase 8 — Platform Engineering & Scale** — HTTPS+ACM, staging env, blue/green deploys, ECS autoscaling, PgBouncer, RDS read replicas, audit_logs/job_events partitioning, feature flags
- **Phase 9 — Security Hardening** — WAF, secret rotation, VPC flow logs, dependency scanning, OWASP headers, mTLS, least-privilege IAM
- **Phase 11 — Real-time Stream Analytics** — Kafka Streams/Flink, ClickHouse, materialized rollups, customer-facing analytics API
- **Phase 13 — Disaster Recovery & Chaos** — multi-region, MirrorMaker 2, cross-region replicas, RPO/RTO SLOs, chaos tests in CI

---

## Distributed systems depth

| Item | Size | Notes |
|---|---|---|
| Outbox → CDC migration via Debezium | L | Replace `_outbox_relay_loop` with a Debezium source connector against the `outbox_events` table. Easier than full CDC because we keep the application-controlled event taxonomy. Sunset the polling relay at the end. |
| Distributed lock manager (Redlock) | M | Single Redis SETNX is enough today. Redlock with multiple Redis nodes is the next step when one Redis cluster isn't enough. Demonstrate via a "lease the job" pattern that prevents double-execution even on broker redelivery. |
| Workflow versioning | L | Deploy v2 of a saga template without breaking in-flight v1 sagas. Add a `version` column to `sagas`; processors register per-version; runtime picks the version the saga was created with. |
| Schema registry as a service | M | Pull the file-based JSON Schema into a tiny standalone service that versions schemas and runs compatibility checks on PR. Producers + consumers fetch by name + version. |
| Sharded workers via consistent hashing | L | Today: one worker process. Tomorrow: N workers, each owning a hash-range of `tenant_id`. The dispatcher partitions on input; same-tenant jobs land on the same worker (sticky processing). Enables per-tenant in-process caching. |
| Cross-region Kafka MirrorMaker 2 | XL | Phase 13 precursor. `job.*` topics mirrored to a second region; consumer offsets translated. Lays the groundwork for active-passive failover. |
| Distributed tracing with PagerDuty-style timeline | M | The OTel data is there; visualize it as a timeline of spans on the admin UI. "This dead-letter happened because the upstream API took 8s and we timed out at 5s" — visible in the UI without leaving for X-Ray. |
| Distributed cache invalidation | S | Redis Pub/Sub channel for cache-bust events. Today the cache is single-region single-Redis so it doesn't matter. When we add read replicas + per-region caches, this is the right tool. |
| Idempotency at the broker level via transactional producer | M | aiokafka exposes `transactional_id`. Wrap the outbox relay's batch in a Kafka transaction so a publish-then-mark-published cycle is exactly-once. Reduces the "redelivered after publish" footprint to zero. |

---

## Backend depth

| Item | Size | Notes |
|---|---|---|
| Public REST API + API keys + scopes | L | Separate auth path from the user JWT. `Authorization: Bearer ipt_xxx` with scopes like `jobs:write`, `audit:read`. Per-tenant API key management UI. |
| Webhooks: subscribe to lifecycle events | M | Tenants register a URL + secret + which events. The worker process publishes to a webhook queue; a separate consumer handles signing + delivery + retries. Failed deliveries DLQ to admin attention. |
| Python SDK | S | Wrap the public API in a tiny pip-installable client. Mostly demonstrative — a 200-line wrapper, generated docs. |
| TypeScript SDK | S | Same as above but for JS/TS callers. Auto-generated from OpenAPI. |
| Go SDK | M | Less of a generated-client story; demonstrates we can ship a credible Go client. |
| gRPC for service-to-service | L | Worker → external service calls today are all HTTP. Add gRPC for the ones we control. Demonstrates protobuf + streaming RPC. |
| Scheduled cron jobs as first-class | M | New `JobType.CRON` plus a `cron_schedule` column. A new background loop reads schedules and enqueues runs. Replaces "use the external scheduler then POST /jobs" with native scheduling. |
| Workflow templates from YAML | L | Saga but declarative. YAML file → spec → durable workflow. Argo Workflows-ish but tenant-scoped. |
| Job priority queue with starvation prevention | M | Priority today is informational — Kafka is FIFO. Add a delayed queue with aging (low-priority jobs gain priority over time) so high-priority storms can't starve low-priority work indefinitely. |
| Bulk operations endpoint | S | `POST /jobs/batch` with up to 1000 jobs in one body. Returns one ID per. The outbox doesn't care, it's just 1000 rows in the same transaction. |
| Conditional dependencies | M | Child runs only if parent's `result.amount > 100`. Tiny expression evaluator (no eval-of-user-input — a typed AST). Useful for branch logic in sagas. |
| Idempotency-key TTL + reuse policy | S | Today keys live forever. Add an expiry so the same key can be reused after 24h. Tenant-configurable. |
| Job pause / resume | M | New `JobStatus.PAUSED`. Pause endpoint marks a job paused mid-progress; resume re-emits a `job.submitted`. Demonstrates how to layer new states without breaking event sourcing (`paused`/`resumed` events added to the topic schemas). |

---

## Frontend depth

| Item | Size | Notes |
|---|---|---|
| Saga DAG visualization | M | ReactFlow or similar. Shows steps as nodes, dependencies as edges, statuses by color. Live-updating as the saga progresses. |
| Command palette (Cmd-K) | M | One keyboard shortcut to fuzzy-jump to any job, tenant, runbook, or admin tab. Implemented with `cmdk`. Sets the senior-tier bar for keyboard navigation. |
| Live metric charts | M | Sparklines on the overview cards; full charts on a new Metrics tab. Pulls from the existing CloudWatch metrics via a new backend endpoint. Recharts or visx for the rendering. |
| Mobile-responsive admin | S | The DLQ tab + the digest tab are the on-call read paths; both should work on phone. Tailwind responsive utilities + a hamburger nav. |
| Embeddable dashboard widget | M | iframe-able status panel for a tenant. Useful for embedding in a customer's intranet. Needs CSP + frame-ancestors discipline + a separate auth path (signed URL with TTL). |
| Dark/light theme | S | Already dark; add a light mode. Tailwind `dark:` is already in use; invert by making the *current* theme the dark variant and adding a class on `<html>` for light. |
| Accessibility audit (WCAG AA) | M | Run axe-core in CI. Fix the things it flags — focus rings, ARIA labels on icon-only buttons, color contrast. Documents a baseline. |
| Job dependency DAG visualization | M | Same as saga DAG but for arbitrary job-dependency graphs. The data is in `job_dependencies`. |
| Drag-and-drop saga builder | L | Visual builder for sagas. Drag job types onto a canvas, draw dependencies, save as a saga template. Complements the YAML templating item above. |
| Browser extension for on-call monitoring | M | Small extension that shows current DLQ count + open incidents in a toolbar badge. Useful for someone who keeps the platform open in a tab. |
| Notification system (in-app) | M | A bell icon. Job state transitions can ping the requester; admin replays can ping the job owner. Real-time via the existing SSE infrastructure. |
| Storybook for the component library | S | Document every shared component. Useful for contributors. |
| Visual regression tests via Playwright | M | Snapshot key pages; fail PRs that change them unintentionally. Catches "I changed Layout and broke something on JobDetail" before code review. |

---

## Security / compliance

| Item | Size | Notes |
|---|---|---|
| OIDC SSO (Auth0, Google, Okta) | M | Add an OIDC client to the auth path. Existing email/password keeps working. Per-tenant config of which providers are allowed. |
| SAML support | L | Enterprise auth. AWS Cognito or a self-hosted SAML library (python-saml). |
| Per-tenant API keys + scopes | M | Companion to the public-API item above. Persistent keys with HMAC verification, scope enforcement at the dependency layer. |
| PII detection + redaction in payloads | M | Microsoft Presidio for detection. Tenant policy: detect-only / redact / block. Detected PII is replaced with `<REDACTED:type>` before persisting. |
| Audit log → SIEM | S | A small Kafka consumer that ships audit events to CloudWatch → Splunk / Datadog. |
| Right-to-erasure (GDPR) endpoint | M | Deletes a user + cascades to their jobs/audit_logs/triages, then audit-logs the deletion. Tricky because we want to preserve the event log for forensics but redact PII. |
| Customer-managed encryption keys (KMS) | L | At-rest encryption with per-tenant KMS keys. JSONB payload columns are the things to encrypt. |
| TOTP / WebAuthn 2FA | M | Add a `mfa_secret` to users; require TOTP on admin login. WebAuthn is the senior tier — passkeys instead of TOTP. |
| Rate-limit by API key (in addition to user) | S | If we ship API keys, they should have their own per-key rate limit. |
| Token revocation list | S | Today JWTs are stateless. A small Redis set of revoked token IDs (`jti`) lets admins force-logout. |
| Login attempt rate limiting + lockout | S | Already have rate limiting on the login route. Add a "5 failures = 15min lockout" policy. |
| Field-level encryption for payloads | M | The `result` and `payload` columns may contain sensitive data. Encrypt with a per-tenant key derived from the master KMS key. |
| Cross-tenant access logging | S | When a platform admin uses `?tenant_id=X` to view another tenant, log it prominently. The audit row should make the cross-tenant access visible. |

---

## AI / ML

| Item | Size | Notes |
|---|---|---|
| Embeddings-based job similarity | M | Anthropic embeddings or OpenAI's. Store in pgvector (Postgres extension). "Show me jobs like this failed one" returns the 10 nearest neighbors. Useful in DLQ triage. |
| Anomaly detection on metrics | M | Z-score over rolling window per tenant. Pages on outliers — "tenant Acme's failure rate is 5σ above their 7-day baseline." Layer on top of the existing CloudWatch metrics. |
| Predictive autoscaling | L | Forecast next-hour queue depth from historical patterns; pre-warm ECS tasks. Demonstrates a simple time-series model in production. |
| LLM-driven workflow generation | L | "Fetch all CSVs from S3 and email summaries" → LLM emits a saga spec. Validated against the workflow template schema before saving. |
| Cost optimizer | M | LLM looks at a tenant's retry patterns and suggests a cheaper backoff strategy or different max_retries. Surfaces as a recommendation on the admin UI. |
| RAG over runbooks for on-call chat | M | Pinecone or pgvector + the existing runbooks. "How do I clear a stuck saga?" → grounded answer. |
| LLM-driven test generation | S | When adding a new processor, LLM scaffolds the pytest file from the processor signature + a description. Developer-facing rather than production. |
| Fine-tune Claude on the company's failure patterns | XL | Way beyond reasonable. Only worth doing once you have thousands of triaged jobs and the in-house team to run fine-tuning. |
| Embedding cache | S | Redis cache of `(text, model) → vector` so repeated text doesn't re-bill. |
| Hybrid search on audit logs | M | Full-text + vector search. The audit log is already structured; layer FTS via Postgres `tsvector` and pgvector for similarity. |
| Auto-translate runbooks | S | Operators in non-English-speaking regions get runbooks in their language. One-shot translation, cached per language. |

---

## Developer experience

| Item | Size | Notes |
|---|---|---|
| CLI tool (`incidentd jobs list ...`) | M | Use the public API (when shipped). Click for the CLI framework. Auto-generated from the OpenAPI spec. |
| Devcontainer | S | `.devcontainer/devcontainer.json` so VS Code Remote opens with the full toolchain in <5 min. |
| Make targets | S | `make test`, `make lint`, `make dev` — the convention every senior project ends up with. |
| Code generators | M | `./scripts/new_processor.py csv_export` scaffolds processor + tests + DLQ runbook. Cuts new-job-type ramp from 30 min to 5. |
| Pre-commit hooks | S | ruff, mypy on changed files, frontend lint. Catches the easy mistakes before CI. |
| Storybook | S | (also under Frontend) |
| Performance regression tracking in CI | M | Run a small benchmark suite per PR; surface regressions in PR comments. |
| ChatOps Slack bot | M | Replay/resolve jobs from Slack. Auth via a per-Slack-user token. Demonstrates a second API surface beyond HTTP. |
| Local LLM mode | M | Optional `claude-haiku` or local Ollama models for development so contributors don't need an API key. |
| Compose profile per scenario | S | `docker compose --profile minimal up` (no Kafka, no MinIO). `--profile full` is current default. Lets you run the API alone for quick frontend dev. |
| Visual diff tool for production runs | M | "How did the most recent successful CSV upload compare to the one before it?" Inputs, outputs, timing — side-by-side. Powerful debugging tool. |

---

## Performance / scale

| Item | Size | Notes |
|---|---|---|
| PgBouncer sidecar | M | Connection pool in front of RDS. Measure before/after on the API process during a load test. |
| Audit log partitioning by month | M | `pg_partman` or a manual partition strategy. Query speedup on time-bounded queries. Phase 8 item formally. |
| job_events partitioning | M | Same as audit logs. The event log is append-only and time-stamped; partitions are a natural fit. |
| Read replicas + read-path routing | L | Route admin analytics queries to a replica via a separate DB URL. The application has to know which queries are safe on the replica. |
| ClickHouse for OLAP | XL | (Phase 11) Customer-facing analytics requires sub-second p99 over millions of events. Kafka → ClickHouse Connect → materialized views. |
| Tracemalloc snapshots in CI | M | Memory regression guard. Run a representative workload; assert peak memory hasn't grown >10% from baseline. |
| Per-job-type load tests | M | Locust scenarios per `JobType`. Catches type-specific regressions. |
| Chaos test: dispatcher killed mid-batch | M | Litmus or a custom asyncio kill-switch. Assert the system recovers within an expected window. |
| Async DB writes for non-critical paths | S | `await session.flush()` blocks. For audit logs and outbox rows we don't need to wait — `asyncio.create_task` the flush. Caveats around transaction boundaries. |
| Bulk insert for outbox | S | When `JobService.create_job` writes one outbox row per job, a batch endpoint writes N. Use `bulk_insert_mappings` or COPY. |
| HTTP/2 between frontend and ALB | S | ALB supports HTTP/2; turn it on. Multiplex SSE + XHR over one connection. |
| Lazy-load admin tabs | S | The bundle is small enough today; once charts + DAG viz land, code-split the admin page. |

---

## Picking a next PR

Suggested heuristic for interview-prep mode:

1. **Pick one item from "Distributed systems depth"** every 3-4 PRs — these are the senior/staff-level moves and showcase the most.
2. **Sprinkle in frontend depth + AI/ML** between to keep the surface varied and demoable.
3. **Hold security and platform engineering for dedicated phases** — they land best when batched.

A representative next-three-PRs sequence:

1. **Webhooks** (backend M) — touches public surface, hooks into existing event stream, demonstrates signed delivery + retries.
2. **Saga DAG visualization** (frontend M) — visually impressive; uses real data; touches the saga subsystem.
3. **Embeddings-based job similarity** (AI/ML M) — adds pgvector to the toolbelt, demonstrates a non-LLM AI feature.
