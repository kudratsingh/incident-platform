"""
Reset mutable eval-run state so live remediation scenarios start from a
known baseline. Platform half of the eval-reset protocol (FIX_PLAN #24);
the commander repo's `make eval-reset` shells into this script.

What gets cleared/reset:

  1. **`chaos:*` Redis keys** — kill flags, injected latency, any
     scenario-set chaos state. Same effect as running
     `restart_consumer_group` for every affected consumer, plus a
     scan-and-delete for any chaos keys the platform doesn't yet
     have a Tier-1 compensator for.
  2. **Eval fixtures** — delegates to `seed_eval_fixtures.seed(reset=True)`
     which restores DLQ job status/retry_count/hint, re-populates the
     consumer-lag keys, and seeds the `hot_set` fixture (FIX_PLAN #7,
     #19).
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
     `chaos:%` and is still active gets `resolved_at` stamped, returning
     the active-alert surface to the seeded baseline. `bad_deploy` is
     the only producer today; without this the alert it fires is never
     resolved by anything, so each invocation permanently adds one more
     active `critical` alert to every later alert-count/noise scenario.
     Resolved, never deleted — the alert id appears in the invoking
     scenario's output and trajectories.
  6. **Idempotency records** — with `--purge-idempotency`, `DELETE`s
     every `idempotency_records` row for the seeded incident-commander
     service account. Off by default; the 24h TTL from [ADR 0010]
     handles the common case, but opt-in purge is useful when a
     scenario needs a guaranteed-fresh cache.

## Guardrails

- **Refuses to run in production.** `_assert_not_production()` is the
  single check, and it is enforced on *both* entry points: `main()`
  turns it into a stderr message + `exit(1)`, and `reset()` re-raises it
  as a `RuntimeError` before any engine or Redis client is constructed.
  Gating only the CLI was the bug (D-08) — `reset()` is exported, takes
  arbitrary DB/Redis URLs, and is what the eval harness imports. Same
  belt-and-braces reasoning as ADR 0008: two independent gates on the
  same invariant.
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
_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (os.path.join(_HERE, "..", "backend"), os.path.join(_HERE, "..")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import redis.asyncio as aioredis  # noqa: E402
from app.config import get_settings  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    async_sessionmaker,
    create_async_engine,
)

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

# Tier-1 *action* residue, as opposed to chaos residue above. These are
# effects the agent itself creates during a scenario; left in place they
# fire or apply during the next one. Mirrors
# `dlq_replay_scheduler.SCHEDULED_KEY` — kept as a literal so this
# script has no import dependency on the worker package.
_SCHEDULED_REPLAY_KEY = "jobs:dlq_replay_delayed"


def _empty_dlq_baseline() -> bool:
    """Whether the inter-scenario baseline is an empty DLQ.

    Read at call time, not import time, so a single process can be
    exercised both ways in tests."""
    return os.getenv("EVAL_EMPTY_DLQ_BASELINE", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


async def _clear_chaos_keys(redis: aioredis.Redis) -> int:
    """Scan + delete every key matching a chaos pattern. Uses SCAN over
    KEYS so a large keyspace doesn't block Redis."""
    deleted = 0
    for pattern in _CHAOS_KEY_PATTERNS:
        cursor = 0
        while True:
            cursor, batch = await redis.scan(cursor=cursor, match=pattern, count=100)
            if batch:
                deleted += await redis.delete(*batch)
            if cursor == 0:
                break
    return deleted


async def _clear_scheduled_replays(redis: aioredis.Redis) -> int:
    """Drop every pending delayed-DLQ-replay timer.

    `replay_dlq_by_ids/-by_category(delay_seconds=...)` pushes onto the
    `jobs:dlq_replay_delayed` ZSET and a worker loop fires it when the
    delay elapses. Nothing cancelled those on reset, so a scenario that
    scheduled a 5-minute replay left a live timer behind: it fires
    mid-*next*-scenario, replays a DLQ entry nobody asked about, and
    the DLQ shrinks under the next agent's feet.

    Returns the number of timers removed."""
    pending = int(await redis.zcard(_SCHEDULED_REPLAY_KEY) or 0)
    if pending:
        await redis.delete(_SCHEDULED_REPLAY_KEY)
    return pending


async def _clear_dag_pauses(redis: aioredis.Redis) -> int:
    """Delete every `dag:paused:*` flag.

    Harmless before v0.4.9, when the flag was written and never read.
    Now that the DependencyResolver enforces it (ADR 0011), a pause
    left over from one scenario holds the next scenario's DAG in
    WAITING — the same class of cross-scenario bleed as the replay
    timers above.

    Returns the number of pause flags removed."""
    deleted = 0
    cursor = 0
    while True:
        cursor, batch = await redis.scan(
            cursor=cursor, match="dag:paused:*", count=100
        )
        if batch:
            deleted += await redis.delete(*batch)
        if cursor == 0:
            break
    return deleted


async def _purge_idempotency_records(session_factory: Any) -> int:
    """DELETE every idempotency_records row for the seeded
    incident-commander SA. Scoped to that principal so we don't wipe
    unrelated agents' state (if any exist)."""
    from app.models.service_account import ServiceAccount
    from sqlalchemy import select

    async with session_factory() as session:
        async with session.begin():
            sa = (
                await session.execute(
                    select(ServiceAccount).where(
                        ServiceAccount.name == "incident-commander"
                    )
                )
            ).scalar_one_or_none()
            if sa is None:
                return 0
            result = await session.execute(
                text(
                    "DELETE FROM idempotency_records "
                    "WHERE principal_id = :pid"
                ),
                {"pid": sa.id},
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


def _assert_not_production() -> None:
    """Raise if invoked against a production platform. Backed by ADR
    0008's environment gate — this is the belt on top of the braces.

    The single gate, shared by both entry points: `reset()` lets the
    `RuntimeError` propagate to its (library) caller, `main()` turns it
    into a stderr message and `exit(1)` for the CLI. There is
    deliberately no `allow_production` parameter — overriding
    `ENVIRONMENT` is the one documented escape hatch, and one lever is
    enough."""
    env = get_settings().environment
    if env == "production":
        raise RuntimeError(
            "reset_eval_state.py refuses to run in production "
            f"(ENVIRONMENT={env!r}). If this is a real production-parity "
            "eval env, override ENVIRONMENT before invoking."
        )


def _refuse_in_production() -> None:
    """CLI wrapper around `_assert_not_production()`: loud message on
    stderr, exit code 1."""
    try:
        _assert_not_production()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


async def reset(
    *,
    database_url: str = _DB_URL,
    redis_url: str = _REDIS_URL,
    purge_idempotency: bool = False,
) -> dict[str, Any]:
    """Programmatic entry point. Returns a summary dict suitable for
    JSON encoding by the CLI or the eval harness.

    Raises `RuntimeError` when `ENVIRONMENT=production`, before anything
    is imported, connected or created — this coroutine accepts arbitrary
    DB/Redis URLs, so the CLI's gate protected nothing here (D-08)."""
    _assert_not_production()

    # Local import so the seed script's heavy DB imports are only paid
    # by scenarios that actually run this reset.
    from scripts import seed_eval_fixtures  # type: ignore[import-not-found]

    engine = create_async_engine(database_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    redis = aioredis.from_url(redis_url, decode_responses=True)

    try:
        chaos_cleared = await _clear_chaos_keys(redis)
        timers_cleared = await _clear_scheduled_replays(redis)
        pauses_cleared = await _clear_dag_pauses(redis)
        # Order-independent of the seed: the seeded fixture alerts use
        # non-chaos sources, so this can neither race nor re-resolve them.
        chaos_alerts_resolved = await _resolve_chaos_alerts(factory)
        seed_summary = await seed_eval_fixtures.seed(
            database_url=database_url,
            redis_url=redis_url,
            reset=True,
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
        idempotency_purged = 0
        if purge_idempotency:
            idempotency_purged = await _purge_idempotency_records(factory)
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
        "empty_dlq_baseline": _empty_dlq_baseline(),
        "seeded_dlq_deleted": seeded_dlq_deleted,
        "idempotency_purged": idempotency_purged,
        "timers_cleared": timers_cleared,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Reset mutable eval state so live remediation scenarios "
            "start from baseline. Refuses to run against ENVIRONMENT=production."
        )
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

    _refuse_in_production()
    summary = await reset(purge_idempotency=args.purge_idempotency)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
