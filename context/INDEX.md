# Session index

One line per session. **Read this before starting work**; it is cheaper than rediscovering.

`STATE.md` at the workspace root says where things *are*. This says how they got there, which is
the part that stops you from re-litigating a settled decision or re-investigating a closed
question.

Most of the campaign history is agent-side and lives in
`../incident-commander/context/INDEX.md`. This file carries what happened to **the platform**.
Read both — the interesting failures have been on the seam between them.

An archive listed as *transcript only* means the raw session data is on disk under
`~/.claude/projects/-Users-kudratsingh-Documents-audit-ws/` but was never packed. Pack it with
`./context/pack.sh <slug>` if you need it to survive.

| Date | Archive | What this session established |
|---|---|---|
| 2026-08-08 → 08-10 | *transcript only* | **The audit.** Platform's half of 129 defects across both repos. `AUDIT_REPORT.md` at the workspace root. |
| 2026-08-10 → 08-12 | *transcript only* | **The fix campaign and v0.5.0.** Platform work orders merged, tag cut, image published. Tool surface went 26 → 27 tools; the rebless diff was verified to the leaf as exactly 10 scalar deltas and zero structural changes. |
| 2026-08-12 | *transcript only* | **ECS deploy gated** (PR #96). The deploy job now requires `master` + `push` + `vars.ENABLE_ECS_DEPLOY == 'true'`, so a fork or a stray branch cannot reach the cluster. |
| 2026-08-13 | *transcript only* | **Seeding bug found from the agent side.** `SEED_EVAL_FIXTURES` was set on the `platform` service, which overrides `command:` to run the standalone MCP process and so never executes the REST app's startup hook that reads the flag. Nothing had ever seeded the demo stack. |
| 2026-08-16 | `2026-08-16-campaign-backfill.zip` | **This convention, plus a backfill.** `context/` added to both repos; the whole campaign's 175 transcripts packed. The archive is identical in both repos — transcripts are workspace-level, not repo-level, and it predates the convention. Future archives go in whichever repo the session worked in. No product code changed. |
| 2026-08-21 | *transcript only* | **`get_cache_key_info` read tool.** Closes the readiness-sweep finding that `create_stale_cache` writes a Redis key no read tool can see. Exact-key existence / TTL / type / size (never the value), `telemetry:read`, allowlisted to the same four prefixes `invalidate_cache_key` may delete; subset relations tripwired in `test_cache_key_allowlist.py`. Chaos-enabled MCP surface 27 → 28 — the next re-pin's rebless diff gains `+get_cache_key_info`. |
| 2026-08-21 | *transcript only* | **`create_stuck_dag` chaos hook.** `remediate_runaway_saga_success` could not run honestly live: the boot-seeded DAG auto-completes ~10s after boot, so a live run graded alert-credulity, not remediation. The hook manufactures `upstream (completed) → root (dead_letter) → N (waiting)` — stuck by the platform's own rules, since the resolver promotes only when every parent is `completed` and `dead_letter` is terminal. Observable via `get_dag_state`; compensators `replay_dlq_by_ids` (unsticks) and `pause_dag` (stabilizes) per the ADR 0008 pairing rule. Chaos-enabled MCP surface 28 → 29 — the rebless diff gains `+create_stuck_dag`. |
| 2026-08-30 | *transcript only* | **The integration tier was never running.** `testpaths` scoped bare `pytest` to unit+api and the three `RUN_*` gates were exported nowhere, so all 25 tests in `backend/tests/integration/` — the only proofs of RLS tenant isolation, audit-log immutability and outbox single-writer exclusivity — had never executed in CI. Added the `integration` job (Testcontainers, all gates set, census step that fails on any skip) + `make test-integration`, and popped `ALEMBIC_DATABASE_URL` in both `_alembic()` helpers so an inherited value can't redirect the destructive migration cycle. Also deleted the `test` job's Postgres/Redis service containers, which nothing collected had ever read. |
| 2026-08-30 → 09-07 | *transcript only* | **What the first paid live run taught the platform.** Three things, all found by running the commander against a real stack rather than by reading code. (1) *RLS fail-closed works, and the proof is a refusal.* The hand cleanup of stray alerts was rejected when issued as an unscoped `UPDATE alerts …` over the `incident_app` role — exactly the wave-11 R2-129 behaviour, arriving unprompted on a statement nobody had written a test for. It is the only positive evidence we have that the policy binds writes the campaign never anticipated; the cleanup went through as superuser instead. (2) *The SLO evaluator counts the eval fixtures, so a freshly booted eval world alerts on itself.* 4 failed of 7 seeded jobs is 42.9% against a 99% objective — a 14.4× fast burn by construction, raised within one `SLO_EVALUATION_INTERVAL_SECONDS` (300s default) of boot, and re-raised every hour because `_fast_burn_dedup_key` buckets by hour. Mitigated commander-side for the run by setting `SLO_EVALUATION_INTERVAL_SECONDS=0` in the eval compose; the platform fix — exclude fixture-seeded rows from the SLI denominators — is filed as **WO-R2-132**. (3) *`reset_eval_state.py` does not return the alert surface to baseline.* Its sweep predicate is `source LIKE 'chaos:%'` and the SLO loop writes `source = 'slo:<id>'`, so organic alerts survive every reset and accumulate as agent-visible distractors — filed as **WO-R2-131**. |

## Things a future session should not have to rediscover

- **The digest that matters is the index digest, not the child.** `docker manifest inspect -v`
  returns a list whose `[0]` is the linux/amd64 *child* manifest. Pinning that is wrong and the
  mistake was made once — the commander must pin `sha256:8b57d0c9…`, the index. Verify with
  `docker buildx imagetools inspect`, which shows both and labels them.
- **`tools/list` needs no rows.** The only CI job that boots the stack diffs the tool schemas, so
  an empty database is indistinguishable from a fully seeded one. This is why the seeding bug
  survived the entire project. Any check meant to catch missing *data* has to actually read data.
- **Severity is a closed enum and 32 of the commander's 38 scenario alerts violate it** — they
  send `high` / `medium` / `low`, which the platform rejects. Not a platform bug, but it is the
  platform's enum that decides it, so a change here re-calibrates every scenario over there.
- **`AlertPayload` declares two fields the webhook never sends.** This is the mechanism behind
  issue #141 (alert dedupe inert in production).
- **Nothing produces a `fingerprint`.** All 38 commander scenarios pin one and the agent derives
  incident identity from it, but the `alerts` table has no such column and the webhook payload has
  no such key. The ingress contract the eval exercises has no producer behind it.

## Open, not blocking

- [#141](https://github.com/kudratsingh/incident-platform/issues/141) — alert dedupe inert in production.
- [#142](https://github.com/kudratsingh/incident-platform/issues/142) — webhook v2 signing.

## Standing rules that outlive any session

- **The commander is an external client.** No shared code imports, no direct connections to this
  platform's Postgres, Redis, or Kafka. A capability the agent needs is a platform PR that adds a
  tool, never a bypass.
- **Chaos hooks are env-gated** and must stay that way.
- **In a git worktree, run pytest with `PYTHONPATH=<worktree>/backend`.** The venv installs the
  backend editable via a `.pth` that hard-points at the *main* checkout, so a full-suite run from a
  worktree root imports `app` from `master` instead of your branch. It looks like a test-ordering
  bug — new tests pass when run from `backend/`, then fail or vanish on the full run — and it cost
  one session its whole budget. CI is unaffected: it checks out a single tree.
- **A green pytest run is not evidence the tier ran.** pytest exits 0 on a fully-skipped module,
  which is how `backend/tests/integration/` stayed invisible for the whole campaign. Three of the
  five files skip unless `RUN_RLS_TEST` / `RUN_EVAL_RESET_TEST` / `RUN_MIGRATION_LOCK_TEST` are
  set; the other two skip without a Docker daemon. Run them with `make test-integration`, never
  bare `pytest backend/tests/integration/`. The `integration` CI job parses its own JUnit report
  and fails if any test skipped — keep that step, it is the only thing standing between this tier
  and a second silent decade.
- **The demo stack is shared state.** `docker compose -f demo/compose.yml down` keeps volumes;
  only `make demo-destroy CONFIRM=1` deletes them. An agent trimming a Kafka topic here once
  crash-looped three consumer groups.
