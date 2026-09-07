"""
Reset mutable eval-run state so live remediation scenarios start from a
known baseline. Platform half of the eval-reset protocol (FIX_PLAN #24);
the commander repo's `make eval-reset` shells into this script.

What gets cleared/reset:

  1. **`chaos:*` Redis keys** — kill flags, injected latency, any
     scenario-set chaos state. Same effect as running
     `restart_consumer_group` for every affected consumer, plus a
     scan-and-delete for any chaos keys the platform doesn't yet
     have a Tier-1 compensator for. Plus `cache:job:*` — the one
     namespace chaos residue could reach without a `chaos:` marker
     (R2-20); it is a 10s read-through cache, so dropping it costs a
     single Postgres read.
  2. **Eval fixtures** — delegates to `seed_eval_fixtures.seed(reset=True)`
     which restores DLQ job status/retry_count/hint, re-populates the
     consumer-lag keys, and seeds the `hot_set` fixture (FIX_PLAN #7,
     #19). Since BUILD_PLAN 2.5 it also re-anchors every time-anchored
     fixture column — `jobs.created_at`/`updated_at`, `alerts.fired_at`/
     `resolved_at`, `deploy_markers.deployed_at` — to its seed-time
     offset from *now*, so age-sensitive scenarios
     (`search_traces(since_hours=...)` and friends) don't watch the
     seeded world go stale as the stack ages. Only stable() fixture
     rows are shifted; relative spacing between them is preserved.
  3. **Tier-1 action residue** — pending delayed-replay timers on the
     `jobs:dlq_replay_delayed` ZSET, and any `dag:paused:*` flag. Both
     are effects the *agent* left behind rather than chaos state, and
     both bleed into the next scenario: a timer fires mid-run and
     shrinks the DLQ unprompted, a stale pause holds the next DAG in
     WAITING (enforced since ADR 0011).
  4. **Non-fixture DLQ rows** — any `dead_letter` job outside the
     `_dlq_specs()` stable-ID set is moved to `cancelled`, so the DLQ
     a scenario sees is exactly the fixture set it was graded against.
     Catches chaos jobs attached to a real user, which the
     chaos-owner-user cleanup below can't reach. Also de-noises the
     planner on non-DLQ scenarios, which read the same surface.
  5. **Chaos-fired alerts** — every `alerts` row whose source matches
     `chaos:%` and is still active gets `resolved_at` stamped. Without
     this the alert `bad_deploy` fires is never resolved by anything, so
     each invocation permanently adds one more active `critical` alert
     to every later alert-count/noise scenario. Resolved, never deleted
     — the alert id appears in the invoking scenario's output and
     trajectories.

     This clears the *chaos* alerts, not the alert surface. Since
     WO-R2-29 the scheduled SLO evaluator is a second producer, writing
     `source = 'slo:<objective-id>'`, which `chaos:%` does not match —
     so an organic fast-burn alert survives every reset and accumulates
     exactly the way `bad_deploy`'s used to. Restoring the *seeded
     baseline* is therefore still an open gap: **WO-R2-131**.
  6. **Idempotency records** — with `--purge-idempotency`, `DELETE`s
     every `idempotency_records` row for the seeded incident-commander
     service account. Off by default; the 24h TTL from [ADR 0010]
     handles the common case, but opt-in purge is useful when a
     scenario needs a guaranteed-fresh cache.
  7. **CQRS read-model** — rebuilds `jobs:tenant:*` / `jobs:user:*`
     from the `jobs` table (`read_model.rebuild_read_model`), last, so
     it projects the rows as this reset finally leaves them. The
     projection only moves when a Kafka event names a job, so anything
     a scenario evicted out of it — `saturate_redis` is the tool that
     does this on purpose — stayed missing from the admin overview for
     every later scenario (WO-R2-56). This is the only reset step that
     *recomputes* a surface rather than clearing one.

## Guardrails

- **Refuses to run against a target it was not configured for.**
  `_assert_not_production()` delegates to
  `eval_safety.assert_safe_target()`, which checks *two* things: the
  `ENVIRONMENT=production` label, and — the one that actually matters
  here — that the `database_url`/`redis_url` about to be destroyed are
  the ones `settings` names. The label check alone was the bug
  (WO-R2-18): it inspects the local process while every `DELETE` below
  runs against whatever DSN the caller passed, so an operator in a
  `development` shell passed the gate and emptied whatever
  `DATABASE_URL` pointed at. Pass `--i-know-what-im-doing` (CLI) or
  `allow_target_mismatch=True` (library) when the mismatch is
  deliberate.
- Enforced on *both* entry points: `main()` turns a refusal into a
  stderr message + `exit(1)`, and `reset()` re-raises it as a
  `RuntimeError` before any engine or Redis client is constructed.
  Gating only the CLI was the earlier bug (D-08) — `reset()` is
  exported, takes arbitrary DB/Redis URLs, and is what the eval harness
  imports. Same belt-and-braces reasoning as ADR 0008: independent
  gates on the same invariant.
- **Audit rows are ground truth and this script never touches them.**
  No statement here reads, writes, updates or deletes `audit_logs`. The
  job and user DELETEs it does perform (`_delete_seeded_dlq_fixtures`,
  `_delete_chaos_owner_users`) have one documented side effect: the FKs are
  `ON DELETE SET NULL`, so `audit_logs.job_id` / `audit_logs.user_id` go
  NULL for rows referencing deleted scaffolding (and `job_triages`
  CASCADE-deletes with its job). The audit row itself, its `action`,
  its `extra_data` and its **`resource_id`** all survive intact — which
  is why `resource_id` (a string, written as `str(job_id)` by every
  Tier-1 audit writer) is the durable join key for any audit-based
  grading or forensics. Never join on the FK columns. See the "reset
  disposal vs audit ground truth" amendment in
  [ADR 0012](../docs/ADR/0012-the-lab-is-invisible-to-the-agent.md).
- **Idempotent.** Second run against the same post-reset state is a
  no-op summary.

## Usage

    docker compose exec app python /app/scripts/reset_eval_state.py
    docker compose exec app python /app/scripts/reset_eval_state.py --purge-idempotency

Emits a JSON summary to stdout so the caller (`make eval-reset`) can
parse it into the eval report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

# Allow running from project root without installing the package.
#   * `../backend` so `import app...` resolves.
#   * `..` (the repo/app root) so `from scripts import seed_eval_fixtures`
#     resolves. Without it the script only ran under an explicit
#     `-e PYTHONPATH=/app:/app/backend` override, which is the workaround
#     the commander's `make eval-reset` was carrying. Setting both here
#     makes the shipped image self-sufficient: `docker compose exec app
#     python /app/scripts/reset_eval_state.py` now works unadorned.
#   * `_HERE` itself so the sibling `eval_safety` helper resolves whether
#     this module was imported flat (the unit tests put `scripts/` on
#     `sys.path`) or as `scripts.reset_eval_state`.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (
    os.path.join(_HERE, "..", "backend"),
    os.path.join(_HERE, ".."),
    _HERE,
):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import eval_safety  # type: ignore[import-not-found]  # noqa: E402
import redis.asyncio as aioredis  # noqa: E402
from app.core.tenant_scope import platform_session_factory  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

# Match the script that seeds the fixtures — same defaults, same envvars.
_DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/incident_platform",
)
_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Every Redis key namespace the chaos framework writes into.
#
# One pattern, because every chaos key helper MUST live under `chaos:*`:
# `kafka_consumer.kill_key_for()` yields `chaos:kill:{group}` and
# `kafka_consumer.latency_key_for()` yields `chaos:latency:{group}`.
# A new chaos hook adds no pattern here — it keeps its keys inside the
# namespace, and `test_every_chaos_key_helper_lives_under_the_chaos_namespace`
# fails if one ever escapes. (Two `kafka:consumer:*` entries used to sit
# here claiming to mirror those helpers; they matched no key any code
# has ever written — D-13.)
_CHAOS_KEY_PATTERNS = ("chaos:*",)

# The one namespace chaos residue can reach WITHOUT wearing the `chaos:`
# name (R2-20). `create_stale_cache` used to admit any `cache:` key,
# including the live per-job read cache `cache:job:{tenant}:{job}` that
# `app/utils/cache.py::JobCache` owns — so a poisoned entry carried no
# marker the sweep above could key off and outlived the reset for the
# rest of its TTL, 500-ing `GET /jobs/{id}` in the *next* scenario with
# nothing to correlate it to.
#
# The hook now refuses those keys, so this sweep is belt-and-braces for
# an entry poisoned before the fix or by hand. Clearing it costs
# nothing: it is a 10s read-through cache that repopulates from Postgres
# on the next request, which is also why it is swept rather than
# rebaselined.
_JOB_CACHE_PATTERN = "cache:job:*"

# Tier-1 *action* residue, as opposed to chaos residue above. These are
# effects the agent itself creates during a scenario; left in place they
# fire or apply during the next one. Mirror
# `dlq_replay_scheduler.SCHEDULED_KEY` / `.INFLIGHT_KEY` — kept as
# literals so this script has no import dependency on the worker package.
_SCHEDULED_REPLAY_KEY = "jobs:dlq_replay_delayed"
_INFLIGHT_REPLAY_KEY = "jobs:dlq_replay_inflight"


def _empty_dlq_baseline() -> bool:
    """Whether the inter-scenario baseline is an empty DLQ.

    Read at call time, not import time, so a single process can be
    exercised both ways in tests."""
    return os.getenv("EVAL_EMPTY_DLQ_BASELINE", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


async def _scan_delete(redis: aioredis.Redis, pattern: str) -> int:
    """Delete every key matching `pattern`. Uses SCAN over KEYS so a
    large keyspace doesn't block Redis. Returns the number deleted."""
    deleted = 0
    cursor = 0
    while True:
        cursor, batch = await redis.scan(cursor=cursor, match=pattern, count=100)
        if batch:
            deleted += await redis.delete(*batch)
        if cursor == 0:
            break
    return deleted


async def _clear_chaos_keys(redis: aioredis.Redis) -> int:
    """Scan + delete every key matching a chaos pattern."""
    deleted = 0
    for pattern in _CHAOS_KEY_PATTERNS:
        deleted += await _scan_delete(redis, pattern)
    return deleted


async def _clear_job_read_cache(redis: aioredis.Redis) -> int:
    """Drop every live per-job read-cache entry — see `_JOB_CACHE_PATTERN`
    for why chaos residue can land here without a `chaos:` marker.

    Returns the number of entries removed."""
    return await _scan_delete(redis, _JOB_CACHE_PATTERN)


async def _clear_scheduled_replays(redis: aioredis.Redis) -> int:
    """Drop every pending delayed-DLQ-replay timer, armed or claimed.

    `replay_dlq_by_ids/-by_category(delay_seconds=...)` pushes onto the
    `jobs:dlq_replay_delayed` ZSET and a worker loop fires it when the
    delay elapses. Nothing cancelled those on reset, so a scenario that
    scheduled a 5-minute replay left a live timer behind: it fires
    mid-*next*-scenario, replays a DLQ entry nobody asked about, and
    the DLQ shrinks under the next agent's feet.

    The in-flight set is swept for the same reason (R2-21). A timer the
    worker has claimed but not yet acked is still a pending replay — and
    an un-acked claim is *designed* to be recovered on a later tick, so
    leaving it behind would resurrect exactly the bleed this clears.

    Returns the number of timers removed across both sets."""
    pending = 0
    for key in (_SCHEDULED_REPLAY_KEY, _INFLIGHT_REPLAY_KEY):
        held = int(await redis.zcard(key) or 0)
        if held:
            await redis.delete(key)
            pending += held
    return pending


async def _clear_dag_pauses(redis: aioredis.Redis) -> int:
    """Delete every `dag:paused:*` flag.

    Harmless before v0.4.9, when the flag was written and never read.
    Now that the DependencyResolver enforces it (ADR 0011), a pause
    left over from one scenario holds the next scenario's DAG in
    WAITING — the same class of cross-scenario bleed as the replay
    timers above.

    Returns the number of pause flags removed."""
    return await _scan_delete(redis, "dag:paused:*")


async def _rebuild_read_model(session_factory: Any, redis: aioredis.Redis) -> int:
    """Recompute the CQRS read-model keys from the `jobs` table.

    The projection (`jobs:tenant:*` / `jobs:user:*`) is derived state that
    only moves when a Kafka event names a job, so it has no way to heal
    itself: whatever a scenario's `saturate_redis` evicted, or a Redis
    restart dropped, stays missing from the admin overview for every
    later scenario. Every other reset step above restores a surface the
    scenarios read; before WO-R2-56 this one was simply left broken.

    Runs last, so it projects the job rows as this reset finally leaves
    them — after the DLQ fixtures are restored, the non-fixture DLQ is
    swept to `cancelled`, and the chaos-owner users (and their jobs) are
    deleted. Rebuilding earlier would faithfully project rows that the
    steps below it were about to change.

    Returns the number of keys written.
    """
    from app.workers.read_model import rebuild_read_model

    async with session_factory() as session:
        summary = await rebuild_read_model(session, redis)
    return int(summary["keys"])


async def _purge_idempotency_records(session_factory: Any) -> int:
    """DELETE every idempotency_records row for the seeded
    incident-commander SA. Scoped to that principal so we don't wipe
    unrelated agents' state (if any exist).

    **Every** matching principal, not `scalar_one_or_none()`.
    `service_accounts.name` is unique *per tenant*, not globally — a
    second tenant that also seeded an `incident-commander` account made
    `scalar_one_or_none()` raise `MultipleResultsFound`, and it raised
    from the tail of `reset()` where every destructive step had already
    committed: rows deleted, no summary printed, non-zero exit
    (WO-R2-18). Two tenants each running the eval stack against one
    database is the normal shape of a shared dev environment, so this
    was reachable without anything unusual happening.

    Purging all of them is the right reading of the intent as well as
    the safe one: the docstring's promise is "the seeded commander's
    cached responses", and each tenant's copy is exactly that. Other
    principals — `some-other-agent` and friends — are still untouched,
    which is the property the integration test pins.

    The DELETE is the ORM construct rather than this module's usual
    `text()`, because the bind is a list of UUIDs and only the mapped
    column type knows how to render one on both dialects — raw SQL binds
    the Python `UUID` straight through, which asyncpg accepts and the
    SQLite unit harness rejects outright. Every other statement here
    stays raw; this one has a typed parameter and so it is the one that
    can't be.

    Returns the number of rows deleted, across all matching accounts."""
    from app.models.idempotency import IdempotencyRecord
    from app.models.service_account import ServiceAccount
    from sqlalchemy import delete, select

    async with session_factory() as session:
        async with session.begin():
            principal_ids = (
                (
                    await session.execute(
                        select(ServiceAccount.id).where(
                            ServiceAccount.name == "incident-commander"
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not principal_ids:
                return 0
            # The IN list is one entry per tenant holding a commander
            # account, so it is small and bounded.
            result = await session.execute(
                delete(IdempotencyRecord).where(
                    IdempotencyRecord.principal_id.in_(principal_ids)
                )
            )
            return int(result.rowcount or 0)


async def _delete_chaos_owner_users(session_factory: Any) -> int:
    """DELETE users lazy-created by `create_bad_data_job` when a
    tenant had no real users (see PR #83 / FIX_PLAN #8). Recognisable
    by the `chaos-owner+` email prefix + `is_active=false`.

    Owned chaos jobs are removed first so the FK holds. Only touches
    jobs where the user_id is one we're about to delete — real jobs
    that happen to share tenant are unaffected.

    Returns the number of users deleted."""
    async with session_factory() as session:
        async with session.begin():
            # Delete chaos jobs first (FK dependency).
            await session.execute(
                text(
                    "DELETE FROM jobs WHERE user_id IN ("
                    "  SELECT id FROM users "
                    "  WHERE email LIKE 'chaos-owner+%@chaos.local' "
                    "  AND is_active = false"
                    ")"
                )
            )
            result = await session.execute(
                text(
                    "DELETE FROM users "
                    "WHERE email LIKE 'chaos-owner+%@chaos.local' "
                    "AND is_active = false"
                )
            )
            return int(result.rowcount or 0)


async def _resolve_chaos_alerts(session_factory: Any) -> int:
    """Stamp `resolved_at` on every still-active chaos-fired alert.

    The compensating action for `bad_deploy`
    (`app/mcp/tools/chaos/bad_deploy.py`), per the ADR 0008 v0.4.5
    amendment: the hook fires a `critical` alert with source
    `chaos:bad_deploy` and nothing else on the platform ever resolves it
    — `AlertService` only has `create_alert`, and no REST or MCP surface
    touches resolution. So every invocation left one more permanently
    active critical alert behind, contaminating the alert-count and
    noise scenarios of every campaign that followed.

    Scope, stated exactly: this resolves chaos alerts. It does **not**
    return the active-alert surface to the seeded baseline, and has not
    since WO-R2-29 gave the platform a second alert producer. The
    scheduled SLO evaluator writes `source = f"slo:{definition.id}"`
    (`app/services/slo.py`), which `chaos:%` does not match, so an
    organic fast-burn alert is invisible to this sweep and survives
    every reset — the same permanent-distractor failure this function
    was written to end, reintroduced through a source string it does not
    cover. Widening the predicate (or resolving everything and letting
    the fixture reseed put the seeded alerts back) is **WO-R2-131**.
    Until that lands, a live campaign must check the alert surface by
    hand between scenarios.

    Resolve rather than DELETE: the alert id is quoted in the invoking
    scenario's output and trajectories, so deleting would mutate history
    a graded run refers to. `resolved_at` is the model's designed
    off-switch — `AlertRepository.list_active_for_tenant` filters on
    `resolved_at IS NULL`, so a resolved row drops out of the surface the
    agent reads while staying auditable.

    The predicate is `source LIKE 'chaos:%'`, which provably spares the
    5 seeded fixture alerts (`seed_eval_fixtures._alert_rows` uses
    sources `kafka`/`dlq`/`api`/`db`). `CURRENT_TIMESTAMP` rather than
    `now()` so this stays runnable on the SQLite unit harness.

    Idempotent: a second run finds nothing active and returns 0.

    Round-trip test:
    `backend/tests/unit/test_eval_reset.py::test_resolve_chaos_alerts_clears_bad_deploy_residue`.

    Returns the number of alerts resolved."""
    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                text(
                    "UPDATE alerts SET resolved_at = CURRENT_TIMESTAMP "
                    "WHERE source LIKE 'chaos:%' "
                    "AND resolved_at IS NULL"
                )
            )
            return int(result.rowcount or 0)


async def _delete_seeded_dlq_fixtures(session_factory: Any) -> int:
    """DELETE rows created by the `seed_dlq_messages` chaos hook.

    Deleted rather than cancelled (the disposal `_sweep_nonfixture_dlq`
    applies to everything else) because these are scaffolding a
    scenario declared for itself, not history belonging to a real user.
    Cancelling them would leave thousands of dead rows behind across
    eval runs. See ADR 0012 rule 2.

    **The marker contract.** `seed_dlq_messages` writes exactly
    `payload = {SEEDED_FIXTURE_MARKER: True}` — a *top-level* key holding
    boolean `true` (`app/mcp/tools/chaos/seed_dlq_messages.py`). The
    predicate matches that structure and nothing else. It used to be
    `CAST(payload AS text) LIKE '%"seeded_fixture"%'`, which is a
    substring test against the serialized payload: it also matched the
    marker as a *value* (`{"tag": "seeded_fixture"}`), at any nesting
    depth, and with any value at all — including `false`. That is a hard
    DELETE, in any tenant, CASCADE-ing `job_triages` and nulling the
    audit FKs, on a row that merely mentioned the word (S-02).

    Dialect-branched because the predicate has no portable spelling:

      * postgresql — `payload @> '{"seeded_fixture": true}'::jsonb`.
        `PortableJSON` renders as JSONB on PG (`app/models/base.py`), so
        containment is available, and it matches a top-level key with
        boolean true only. Deliberately *not*
        `(payload ->> 'seeded_fixture')::boolean`: a hostile value like
        `{"seeded_fixture": "banana"}` makes that cast raise and aborts
        the whole reset transaction. Containment just returns false.
      * sqlite (the unit harness) — `json_extract(payload,
        '$.seeded_fixture') = 1`, SQLite's spelling of the same test.

    Deliberately **not** scoped by status: a seeded row the agent
    replayed out of `dead_letter` is still declared scaffolding, and
    leaving it behind accumulates exactly the litter ADR 0012's
    delete-don't-cancel rule exists to prevent. Deliberately **not**
    scoped by tenant either: the reset is environment-wide by design
    (see `_sweep_nonfixture_dlq`). Residual risk after the tightening is
    a user who deliberately writes the exact top-level
    `{"seeded_fixture": true}` marker into a real job's payload — that
    row is indistinguishable from scaffolding and will be deleted.

    Audit rows referencing a deleted job are never touched; their
    `resource_id` still carries the job's UUID (module docstring).

    Returns the number of rows deleted."""
    async with session_factory() as session:
        async with session.begin():
            if session.bind.dialect.name == "postgresql":
                statement = text(
                    "DELETE FROM jobs WHERE payload @> CAST(:marker AS jsonb)"
                )
                params: dict[str, Any] = {"marker": '{"seeded_fixture": true}'}
            else:
                statement = text(
                    "DELETE FROM jobs "
                    "WHERE json_extract(payload, '$.seeded_fixture') = 1"
                )
                params = {}
            result = await session.execute(statement, params)
            return int(result.rowcount or 0)


async def _sweep_nonfixture_dlq(session_factory: Any) -> int:
    """Move every `dead_letter` job that isn't a seeded fixture to a
    terminal status, so the DLQ a scenario observes contains exactly
    the fixtures it was graded against.

    `_delete_chaos_owner_users` above only reaches chaos jobs owned by
    a lazy-created `chaos-owner+*` user. When the target tenant already
    had a real user, `create_bad_data_job` attaches the job to *that*
    user instead — so the row survives every reset and accumulates.
    That is how a `bad_data_job` row from 2026-07-31, owned by
    `agent-demo@example.com`, was still sitting in the DLQ days later.

    The cost isn't just a dirty DLQ tab. Stale entries widen the
    surface the agent's planner reads, which pulled it into extra
    probes on scenarios that never mentioned the DLQ — consumer_lag
    included. Sweeping is therefore a de-noising step for the whole
    remediation loop, not just the DLQ scenarios.

    `cancelled` rather than DELETE: these rows are real history for a
    real user, the eval only cares that they're out of `dead_letter`,
    and a status flip stays auditable. Fixtures are identified by the
    stable() ID set — the `eval_fixture` payload marker agrees today,
    but the IDs are what `_reset_dlq_state` re-baselines against, so
    they're the authoritative definition.

    Blast radius is deliberately environment-wide rather than scoped to
    the seeded tenant: a stray `dead_letter` row in *any* tenant is
    visible to a platform-admin-scoped agent and lands in the same
    planner surface. Safe because `_assert_not_production()` runs first
    on *both* entry points — inside `reset()` for library callers and in
    `main()` for the CLI. (That claim used to name `_refuse_in_production()`
    and "this whole script", which was false: the check ran only in
    `main()`, so an importer of `reset()` reached this statement ungated.)

    **Empty-DLQ mode** (`EVAL_EMPTY_DLQ_BASELINE=1`, commander ADR
    0010): the fixture exclusion is dropped and *every* `dead_letter`
    row is swept, so a scenario inherits nothing and declares whatever
    DLQ content it needs via `seed_dlq_messages`.

    Opt-in rather than default on purpose. Flipping the baseline to
    empty is a breaking change for every `dlq_*` scenario currently
    written against the standing 4-row pool. Making it a mode lets the
    platform ship first, the commander migrate its scenarios, and the
    default flip land third — at no point is either side broken. See
    the sequencing note in ADR 0012.

    Returns the number of rows swept."""
    if _empty_dlq_baseline():
        fixture_ids: list[str] = []
    else:
        from scripts import seed_eval_fixtures  # type: ignore[import-not-found]

        fixture_ids = [
            str(spec["job_id"]) for spec in seed_eval_fixtures._dlq_specs()
        ]
    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                text(
                    "UPDATE jobs SET status = 'cancelled', updated_at = now() "
                    "WHERE status = 'dead_letter' "
                    "AND id <> ALL(CAST(:fixture_ids AS uuid[]))"
                ),
                {"fixture_ids": fixture_ids},
            )
            return int(result.rowcount or 0)


def _assert_not_production(
    database_url: str = _DB_URL,
    redis_url: str = _REDIS_URL,
    *,
    allow_target_mismatch: bool = False,
) -> None:
    """Raise if it is not safe to destroy state at this target.

    Two checks, both in `eval_safety.assert_safe_target()`: the
    `ENVIRONMENT=production` label (ADR 0008's environment gate) and,
    since WO-R2-18, the identity of the `database_url`/`redis_url` the
    caller is about to hand every `DELETE` in this module. The label
    alone described the operator's shell, not the database being
    emptied — which is why the arguments default to the module-level
    DSNs but the real callers pass their own.

    The single gate, shared by both entry points: `reset()` lets the
    `RuntimeError` propagate to its (library) caller, `main()` turns it
    into a stderr message and `exit(1)` for the CLI. There is
    deliberately no `allow_production` parameter — overriding
    `ENVIRONMENT` is the one documented escape hatch for *that* check,
    and one lever is enough. `allow_target_mismatch` is the separate,
    per-invocation lever for the target check."""
    eval_safety.assert_safe_target(
        script="reset_eval_state.py",
        database_url=database_url,
        redis_url=redis_url,
        allow_target_mismatch=allow_target_mismatch,
    )


def _refuse_in_production(
    database_url: str = _DB_URL,
    redis_url: str = _REDIS_URL,
    *,
    allow_target_mismatch: bool = False,
) -> None:
    """CLI wrapper around `_assert_not_production()`: loud message on
    stderr, exit code 1."""
    try:
        _assert_not_production(
            database_url,
            redis_url,
            allow_target_mismatch=allow_target_mismatch,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


async def reset(
    *,
    database_url: str = _DB_URL,
    redis_url: str = _REDIS_URL,
    purge_idempotency: bool = False,
    allow_target_mismatch: bool = False,
) -> dict[str, Any]:
    """Programmatic entry point. Returns a summary dict suitable for
    JSON encoding by the CLI or the eval harness.

    Raises `RuntimeError` — before anything is imported, connected or
    created — when `ENVIRONMENT=production`, or when `database_url` /
    `redis_url` are not the ones `settings` names. This coroutine
    accepts arbitrary DB/Redis URLs, so the CLI's gate protected nothing
    here (D-08) and the label-only gate protected nothing either: it
    read the local process while these arguments chose the victim
    (WO-R2-18). `allow_target_mismatch=True` is the deliberate
    override."""
    _assert_not_production(
        database_url,
        redis_url,
        allow_target_mismatch=allow_target_mismatch,
    )

    # Local import so the seed script's heavy DB imports are only paid
    # by scenarios that actually run this reset.
    from scripts import seed_eval_fixtures  # type: ignore[import-not-found]

    engine = create_async_engine(database_url, echo=False)
    # Platform (cross-tenant) scope: this script touches many tenants'
    # rows and sets no `app.tenant_id`. Since WO-R2-129 that is refused
    # rather than silently admitted, and it runs as `incident_app`
    # (docker-compose `app` service) — a non-owner role with no
    # BYPASSRLS — so the declaration is what keeps it working. ADR 0026.
    factory = platform_session_factory(engine)
    redis = aioredis.from_url(redis_url, decode_responses=True)

    try:
        # First, not last. The purge resolves a service account and can
        # raise; run last, that raise landed *after* every destructive
        # step had already committed, so the operator got a traceback
        # and no summary describing what had just been deleted
        # (WO-R2-18). Nothing in the reset creates idempotency records,
        # so the ordering is free — the only thing it changes is which
        # side of a failure the committed damage sits on.
        idempotency_purged = 0
        if purge_idempotency:
            idempotency_purged = await _purge_idempotency_records(factory)
        chaos_cleared = await _clear_chaos_keys(redis)
        job_cache_cleared = await _clear_job_read_cache(redis)
        timers_cleared = await _clear_scheduled_replays(redis)
        pauses_cleared = await _clear_dag_pauses(redis)
        # Order-independent of the seed: the seeded fixture alerts use
        # non-chaos sources, so this can neither race nor re-resolve them.
        chaos_alerts_resolved = await _resolve_chaos_alerts(factory)
        seed_summary = await seed_eval_fixtures.seed(
            database_url=database_url,
            redis_url=redis_url,
            reset=True,
            # The seeder gates its own target too (WO-R2-19). Thread the
            # override through rather than letting it re-decide: this
            # reset already passed the gate for these exact URLs, and a
            # deliberate mismatch that stops halfway through is worse
            # than one that was refused up front.
            allow_target_mismatch=allow_target_mismatch,
        )
        # Delete chaos-owner users from any tenant that had
        # `create_bad_data_job` run against it. Runs unconditionally
        # (unlike the idempotency purge, which is opt-in) — these rows
        # are chaos-specific and shouldn't survive a reset.
        chaos_owners_deleted = await _delete_chaos_owner_users(factory)
        # Runs after the seed/reset above, which restores replayed
        # fixtures to `dead_letter`. Sweeping first would be harmless
        # but pointless — the restore would repopulate the DLQ after
        # the sweep had already looked at it.
        # Delete scenario-declared fixtures before the sweep so they're
        # removed outright rather than left as `cancelled` rows.
        seeded_dlq_deleted = await _delete_seeded_dlq_fixtures(factory)
        dlq_swept = await _sweep_nonfixture_dlq(factory)
        # Last: projects the rows as every step above finally left them.
        read_model_keys = await _rebuild_read_model(factory, redis)
    finally:
        await redis.aclose()
        await engine.dispose()

    return {
        "chaos_alerts_resolved": chaos_alerts_resolved,
        "chaos_keys_cleared": chaos_cleared,
        "chaos_owners_deleted": chaos_owners_deleted,
        "dag_pauses_cleared": pauses_cleared,
        "dlq_reset": seed_summary["dlq_reset"],
        "dlq_swept": dlq_swept,
        "timestamps_rebaselined": seed_summary["timestamps_rebaselined"],
        "empty_dlq_baseline": _empty_dlq_baseline(),
        "job_cache_cleared": job_cache_cleared,
        "read_model_keys_rebuilt": read_model_keys,
        "seeded_dlq_deleted": seeded_dlq_deleted,
        "idempotency_purged": idempotency_purged,
        "timers_cleared": timers_cleared,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Reset mutable eval state so live remediation scenarios "
            "start from baseline. Refuses to run against "
            "ENVIRONMENT=production, or against any DATABASE_URL/REDIS_URL "
            "other than the configured one."
        )
    )
    parser.add_argument(
        "--i-know-what-im-doing",
        dest="allow_target_mismatch",
        action="store_true",
        help=(
            "Proceed even though DATABASE_URL/REDIS_URL are not the "
            "configured ones. This script DESTROYS state at the target; "
            "the flag exists for deliberate cross-stack resets and for "
            "nothing else. It does not override the production check."
        ),
    )
    parser.add_argument(
        "--purge-idempotency",
        action="store_true",
        help=(
            "Also DELETE idempotency_records rows for the seeded "
            "incident-commander service account. Off by default; the "
            "24h TTL (ADR 0010) handles the common case."
        ),
    )
    args = parser.parse_args()

    # Refuse on stderr + exit 1 before anything is connected. `reset()`
    # re-checks the same invariant for library callers.
    _refuse_in_production(
        _DB_URL,
        _REDIS_URL,
        allow_target_mismatch=args.allow_target_mismatch,
    )
    print(eval_safety.describe_target(_DB_URL, _REDIS_URL), file=sys.stderr)
    summary = await reset(
        purge_idempotency=args.purge_idempotency,
        allow_target_mismatch=args.allow_target_mismatch,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
