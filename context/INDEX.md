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
- **The demo stack is shared state.** `docker compose -f demo/compose.yml down` keeps volumes;
  only `make demo-destroy CONFIRM=1` deletes them. An agent trimming a Kafka topic here once
  crash-looped three consumer groups.
