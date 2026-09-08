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

| 2026-09-07 | *transcript only* | **The lab contradicted itself, and an honest agent lost.** Live run `efdc3b2a9864` seeded a stuck chain `remediation_hint=replay_safe` whose root's `error_message` said `SchemaValidationError: payload missing required field 'user_id'`. The agent read both, judged a missing required field permanent, and escalated instead of replaying — sound, and graded a failure. In the lab nothing validates payloads, so the hint was the truth and the text was decoration; the wire said nothing about which to believe. Every lab writer (`seed_dlq_messages`, `create_stuck_dag`, `poison_message`, `create_bad_data_job`, the four seeded rows and their `job_triages` rows) now draws from one table, `app/lab/dlq_failure_stories.py`, with `coherence_violations()` as the rule and a table test walking each writer's pairs through it (**WO-R2-146**). Same PR applies commander #191's four DAG description deltas — `get_dag_state` is no longer "the verification surface for pause_dag", `replay_dlq_by_ids` names the DAG-root un-stick path, `pause_dag` says it is a stabilizer that expires and blocks the fix, `list_dlq_messages` says it is the only read carrying a hint (**WO-R2-141**). Rebless deltas: four description strings plus the error/triage texts on the seeded pack. |
| 2026-09-08 | *transcript only* | **A poisoned message is not safe to replay.** Reviewing the 2026-08-31 trajectory of `remediate_dlq_backlog_success`, the user found the agent replaying a row whose error read `SchemaValidationError: payload missing required field` — because `poison_message` labelled its own dead-letter row `replay_safe`. It always had. WO-R2-146 had already touched this pair and picked the wrong half: it moved the *text* to `UpstreamTimeout …` so the row would agree with its hint, which made the row self-consistent and left it describing a transient fault the hook never injects. **WO-R2-166** moves the hint instead. `poison_message` now writes a schema-violation text under a `NULL` hint by default (nothing has classified a freshly poisoned message — triage is off by default) or `human_required` on request; `replay_safe` is not in its input vocabulary at all. It also gains the `create_bad_data_job` shape — declared `fixture_name`, deterministic id in its own `eeeeeeee-dead-…` namespace, idempotent-until-drifted with the refusal raised *before* the Kafka send, and `payload.seeded_fixture` so the reset DELETEs the row instead of cancelling it. Second half: a new chaos hook `create_mislabeled_dlq_job` writes the lab's one sanctioned incoherent row (hint `replay_safe`, permanent bad-data text) behind a required `mislabel: true`, for a commander scenario whose premise is that the classifier lied — declared outside `DLQ_FAILURE_STORIES` and reachable only through `sanctioned_incoherent_story()`, with a test asserting the coherence screen still flags it. Chaos-enabled MCP surface 29 → 30; rebless gains `+create_mislabeled_dlq_job`, `poison_message`'s two input / three output fields, two 409 refusal codes, and the not-replay-safe behaviour change. |

## Things a future session should not have to rediscover

- **When a fixture's hint and its text disagree, check which half describes what the hook actually
  did before deciding which half to move.** (2026-09-08, WO-R2-166.) WO-R2-146 found
  `poison_message` pairing `replay_safe` with a `SchemaValidationError` and repaired it by moving
  the *text* to a transient one. The pair became coherent and the row became a different lie: the
  hook injects a schema violation and nothing else, so a row claiming an upstream timeout described
  a fault that never happened, and still invited a replay of a payload no replay can fix. The
  coherence screen cannot catch this — it compares two fields to each other, not either field to
  the code. Only a reader asking "what does this hook do?" catches it, which is why the question is
  written down here.
- **A claim about what a caller may safely *do* is held to a higher bar than a claim about what a
  field contains.** `replay_safe` is the strongest routing claim this platform's vocabulary has, and
  it sat on a chaos hook's row for four releases. Same rule, stated for tool descriptions, is now
  the fourth bullet in CLAUDE.md's "Tool descriptions — normative" section.
- **The lab may lie exactly once, by name.** `create_mislabeled_dlq_job` is the only writer allowed
  to produce an incoherent row, because "the classifier was wrong" is a real failure an agent has to
  be measured against. Three guardrails keep the exception from becoming a loophole, and all three
  matter: the pair is declared outside `DLQ_FAILURE_STORIES` so nothing can resolve it from a hint;
  its text is the CSV bad-row one, never the `SchemaValidationError` string from run
  `efdc3b2a9864`, so the promise that *that* pair is never written again stays absolute; and a test
  asserts `coherence_violations` still flags it. Adding a second sanctioned exception should feel
  hard — if it does not, one of the three has been dropped.
- **A lab fixture whose fields disagree grades correct reasoning as wrong.** `remediation_hint` and
  `error_message` arrive together on one `list_dlq_messages` entry, and so does the `job_triages`
  block. Any new lab writer takes its text from `app/lab/dlq_failure_stories.py`; adding a text
  beside a hint by hand is how WO-R2-146 happened. If a wording trips
  `coherence_violations()`, reword it — do not loosen the screen. ADR 0012 says the lab must not
  *name* itself on the agent's surface; this is the same rule one layer in, where it must not
  *contradict* itself either.

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
