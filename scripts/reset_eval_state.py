"""
Reset mutable eval-run state so live remediation scenarios start from a known baseline. Platform
half of the eval-reset protocol (FIX_PLAN #24); the commander's `make eval-reset` shells into this.

What gets cleared/reset:

  1. **`chaos:*` Redis keys** — kill flags, injected latency, any scenario-set chaos state. Plus
     `cache:job:*`, the one namespace chaos residue reaches without a `chaos:` marker (R2-20).
  2. **Eval fixtures** — `seed_eval_fixtures.seed(reset=True)` restores DLQ status/retry_count/
     hint, re-populates the consumer-lag keys, seeds `hot_set` (FIX_PLAN #7, #19) and re-anchors
     every time-anchored fixture column to its seed-time offset from *now*, so age-sensitive
     scenarios don't watch the seeded world go stale. It does **not** restore the seeded DAG
     trio's statuses, and must not: the trio is drained on first boot (the parent is
     `completed`, so the resolver or the 10 s sweep promotes both children), and a row put back
     to `WAITING` behind a `completed` parent races the resume sweep about to promote it again.
     A stranded chain comes from `create_stuck_dag(root_status="completed")` plus
     `kill_consumer('dependency-resolver')` and `pause_control_loop('resume_unblocked_waiting')`
     — ADR 0029, ADR 0027's 2026-09-17 amendment, pinned by
     `backend/tests/unit/test_stranded_chain_and_lab_pause.py`.
  3. **Tier-1 action residue** — delayed-replay timers on `jobs:dlq_replay_delayed`, any
     `dag:paused:*` flag, and the recent-lag window
     (`kafka:consumer_lag:worker-dispatcher:samples`). Each bleeds into the next scenario: a timer
     shrinks the DLQ unprompted, a stale pause holds a DAG in WAITING (ADR 0011), a carried window
     shows a lag trend from the previous run. `pause_dag_chaos` (WO-R3-275, ADR 0029) writes the
     same `dag:paused:*` key the agent's `pause_dag` does, so `dag_pauses_cleared` counts both and
     a leftover pause is no longer evidence the agent acted.
  4. **Declared fixtures and non-fixture DLQ rows** — two disposal classes on purpose (ADR 0012
     rule 2). A row carrying the top-level `seeded_fixture` payload marker is hard-DELETEd;
     everything else still in `dead_letter` outside the `_dlq_specs()` stable-ID set is moved to
     `cancelled`, so the DLQ a scenario sees is the fixture set it was graded against.
  5. **The alert surface** — every still-active `chaos:%` alert is resolved (nothing else resolves
     what `bad_deploy` fires), then every other still-active alert outside the five seeded fixture
     alerts (**WO-R2-131**): the SLO evaluator writes `source = 'slo:<objective-id>'`, which
     `chaos:%` never matched, so organic fast-burn alerts survived every reset and three stray
     ones aborted a paid run pre-spend on 2026-08-31. Resolved, never deleted — the alert id
     appears in scenario output and trajectories.
  6. **Idempotency records** — with `--purge-idempotency`, `DELETE`s every `idempotency_records`
     row for the seeded incident-commander service account. Off by default; ADR 0010's 24h TTL
     handles the common case.
  7. **CQRS read-model** — rebuilds `jobs:tenant:*` / `jobs:user:*` from the `jobs` table
     (`read_model.rebuild_read_model`), last, so it projects the rows as this reset leaves them.
     The projection only moves on a Kafka event, so anything `saturate_redis` evicted stayed
     missing from the admin overview for every later scenario (WO-R2-56).

## Guardrails

- **Refuses to run against a target it was not configured for.** `_assert_not_production()`
  delegates to `eval_safety.assert_safe_target()`: the `ENVIRONMENT=production` label, and that
  `database_url`/`redis_url` are the ones `settings` names. The label alone was the bug
  (WO-R2-18) — it inspects the local process while every `DELETE` runs against the DSN the caller
  passed. Pass `--i-know-what-im-doing` (CLI) or `allow_target_mismatch=True` (library).
- Enforced on *both* entry points: `main()` turns a refusal into stderr + `exit(1)`, `reset()`
  re-raises it before any engine or Redis client exists. Gating only the CLI was D-08.
- **Audit rows are ground truth and this script never touches them.** The job and user DELETEs
  have one documented side effect: the FKs are `ON DELETE SET NULL`, so `audit_logs.job_id` /
  `audit_logs.user_id` go NULL and `job_triages` CASCADEs with its job. `resource_id` survives,
  so it — not the FK columns — is the durable join key for audit-based grading (ADR 0012).
- **Idempotent.** A second run against the same post-reset state is a no-op summary.

## Usage

    docker compose exec app python /app/scripts/reset_eval_state.py [--purge-idempotency]

Emits a JSON summary on stdout for `make eval-reset` to parse; the redacted target goes to stderr
so stdout stays parseable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from typing import Any

# `../backend` for `import app...`, `..` for `from scripts import seed_eval_fixtures` (without
# it the script needed an explicit `-e PYTHONPATH=/app:/app/backend`), and `_HERE` for the sibling
# `eval_safety` whether this module is imported flat or as `scripts.reset_eval_state`.
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

# One pattern, because every chaos key helper MUST live under `chaos:*` —
# `chaos:kill:{group}`, `chaos:latency:{group}`, `chaos:pause:{loop}`. A new hook adds no pattern
# here, and `test_every_chaos_key_helper_lives_under_the_chaos_namespace` fails if one escapes.
_CHAOS_KEY_PATTERNS = ("chaos:*",)

# The one namespace chaos residue can reach WITHOUT a `chaos:` name (R2-20): `create_stale_cache`
# used to admit the live per-job read cache `app/utils/cache.py::JobCache` owns, so a poisoned
# entry outlived the reset and 500-ed `GET /jobs/{id}` in the next scenario. The hook refuses
# those keys now; this sweep is belt-and-braces and free (a 10s read-through cache).
_JOB_CACHE_PATTERN = "cache:job:*"

# Agent-left Tier-1 residue that would fire next scenario. Literal mirrors of
# `dlq_replay_scheduler.SCHEDULED_KEY` / `.INFLIGHT_KEY` — no worker import.
_SCHEDULED_REPLAY_KEY = "jobs:dlq_replay_delayed"
_INFLIGHT_REPLAY_KEY = "jobs:dlq_replay_inflight"

# The metrics loop's window of recent lag measurements for the refreshed group (WO-R3-254). A
# literal mirror of `app/workers/dispatcher.py:LAG_SAMPLES_KEY`, pinned against it by
# `tests/unit/test_consumer_lag_history.py`.
#
# The lag VALUE key beside it is deliberately untouched, in the seeder too: the loop owns it
# under a 90s TTL. The window spans minutes, so measurements from before a reset would be the
# first "trend" the next run sees. Cleared, not rebuilt — the loop records a fresh one within 60s.
_LAG_SAMPLES_KEY = "kafka:consumer_lag:worker-dispatcher:samples"


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

    A timer left on `jobs:dlq_replay_delayed` fires mid-next-scenario and shrinks the DLQ under
    the next agent's feet; an unacked `jobs:dlq_replay_inflight` claim is recovered on a later
    tick and would resurrect the same bleed (R2-21). Returns the count across both sets."""
    pending = 0
    for key in (_SCHEDULED_REPLAY_KEY, _INFLIGHT_REPLAY_KEY):
        held = int(await redis.zcard(key) or 0)
        if held:
            await redis.delete(key)
            pending += held
    return pending


async def _clear_lag_samples(redis: aioredis.Redis) -> int:
    """Drop the recorded consumer-lag measurement window.

    `get_consumer_lag` returns it as `recent_samples`, so one carried across a reset shows the
    next run a trend from the previous one. The value key beside it is left alone."""
    return int(await redis.delete(_LAG_SAMPLES_KEY) or 0)


async def _clear_dag_pauses(redis: aioredis.Redis) -> int:
    """Delete every `dag:paused:*` flag: since ADR 0011 the resolver enforces it, so a pause
    left by one scenario holds the next one's DAG in WAITING."""
    return await _scan_delete(redis, "dag:paused:*")


async def _rebuild_read_model(session_factory: Any, redis: aioredis.Redis) -> int:
    """Recompute the CQRS read-model keys (`jobs:tenant:*` / `jobs:user:*`) from `jobs`.

    Derived state that only moves when a Kafka event names a job, so whatever `saturate_redis`
    evicted cannot heal itself (WO-R2-56). Runs last, projecting the rows as the reset leaves them.
    """
    from app.workers.read_model import rebuild_read_model

    async with session_factory() as session:
        summary = await rebuild_read_model(session, redis)
    return int(summary["keys"])


async def _purge_idempotency_records(session_factory: Any) -> int:
    """DELETE every `idempotency_records` row for the seeded incident-commander SA.

    **Every** matching principal, not `scalar_one_or_none()`: `service_accounts.name` is unique
    per tenant, so a second tenant's copy raised `MultipleResultsFound` from the tail of
    `reset()`, after every destructive step had committed (WO-R2-18). ORM `delete()` rather than
    `text()` because the bind is a list of UUIDs that only the mapped column type can render."""
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
            # One entry per tenant holding a commander account: small and bounded.
            result = await session.execute(
                delete(IdempotencyRecord).where(
                    IdempotencyRecord.principal_id.in_(principal_ids)
                )
            )
            return int(result.rowcount or 0)


async def _delete_chaos_owner_users(session_factory: Any) -> int:
    """DELETE users lazy-created by `create_bad_data_job` (PR #83 / FIX_PLAN #8), recognisable by
    the `chaos-owner+` email prefix plus `is_active=false`. Their jobs go first, for the FK."""
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
    """Stamp `resolved_at` on every still-active alert with `source LIKE 'chaos:%'`.

    The compensating action for `bad_deploy` (ADR 0008's v0.4.5 amendment): it fires a `critical`
    alert nothing else resolves, so every invocation used to leave a permanent distractor behind.
    Chaos only — `_resolve_organic_alerts`, on the next line, is what restores the whole surface.
    Resolved, not DELETEd: the alert id is quoted in scenario output, and
    `AlertRepository.list_active_for_tenant` filters on `resolved_at IS NULL`. The predicate
    spares the five fixture alerts (sources `kafka`/`dlq`/`api`/`db`); `CURRENT_TIMESTAMP` rather
    than `now()` keeps it runnable on SQLite. Idempotent."""
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


def _seeded_alert_ids() -> list[uuid.UUID]:
    """The ids of the five fixture alerts, read from `seed_eval_fixtures._alert_rows`.

    Read rather than restated, so the seed stays the definition of the baseline. The ids are
    `stable()` UUID5s, so the tenant argument does not affect them.
    """
    from scripts import seed_eval_fixtures  # type: ignore[import-not-found]

    return [
        uuid.UUID(str(spec["id"]))
        for spec in seed_eval_fixtures._alert_rows(uuid.uuid4())
    ]


async def _resolve_organic_alerts(session_factory: Any) -> int:
    """Stamp `resolved_at` on every still-active alert that is not one of the five seeded
    fixture alerts. **WO-R2-131.**

    Run C of the paid sequence (2026-08-31) was aborted pre-spend over three stray SLO alerts:
    `_resolve_chaos_alerts` matched `source LIKE 'chaos:%'` and the scheduled evaluator writes
    `source = 'slo:<objective-id>'`. A sweep enumerating known producers is correct only until
    the next one, so this enumerates the **baseline** instead — a closed set of five `stable()`
    ids — and resolves everything else. Spared by id, not by source: an organic alert may
    legitimately reuse `kafka`/`dlq`/`api`/`db`. Resolve, never DELETE, for the sibling's reason.
    Core `update()` rather than `text()` because the exclusion binds five UUIDs, which is exactly
    where the Postgres/SQLite split bites. Idempotent."""
    from app.models.alert import Alert  # type: ignore[import-not-found]
    from sqlalchemy import func, update

    spared = _seeded_alert_ids()
    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                update(Alert)
                .where(
                    Alert.resolved_at.is_(None),
                    Alert.id.not_in(spared),
                )
                .values(resolved_at=func.current_timestamp())
                .execution_options(synchronize_session=False)
            )
            return int(result.rowcount or 0)


async def _delete_seeded_dlq_fixtures(session_factory: Any) -> int:
    """DELETE rows created by a declared-fixture chaos hook — `seed_dlq_messages`,
    `create_stuck_dag`, `create_bad_data_job`, `poison_message`, `create_mislabeled_dlq_job`.

    Deleted rather than cancelled (ADR 0012 rule 2): scaffolding a scenario declared for itself
    under an id it pinned in advance is not a real user's history, and a `cancelled` copy per run
    is litter. Each hook writes a *top-level* `SEEDED_FIXTURE_MARKER` key holding boolean `true`
    (the constant lives in `app/mcp/tools/chaos/seed_dlq_messages.py`), and the predicate is
    containment, not a substring test — the old `CAST(payload AS text) LIKE` form also matched the
    marker as a *value*, at any depth and with any value including `false`, i.e. a hard DELETE on
    a row that merely mentioned the word (S-02). Dialect-branched because containment has no
    portable spelling: `payload @> '{"seeded_fixture": true}'::jsonb` on postgresql (not
    `(payload ->> 'seeded_fixture')::boolean`, which raises on a hostile value and aborts the
    reset), `json_extract(payload, '$.seeded_fixture') = 1` on sqlite. Scoped by neither status
    nor tenant on purpose."""
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
    """Move every `dead_letter` job that is not a seeded fixture to `cancelled`, so the DLQ a
    scenario observes contains exactly the fixtures it was graded against.

    No chaos hook needs this arm any more — since WO-R2-166 every hook that writes a DLQ row
    declares it, and `_delete_seeded_dlq_fixtures` removes those outright whoever owns them. What
    is left is what the lab did not declare: a real dead-letter (processor failure, stale-RUNNING
    recovery, an unregistered `*.compensate`), a pre-marker legacy row, or something written by
    hand. Real history for a real user, hence `cancelled` rather than DELETE — and
    `_delete_chaos_owner_users` cannot reach one attached to a real user. Stale entries also widen
    the planner's surface, which pulled the agent into extra probes on scenarios that never
    mentioned the DLQ.

    Fixtures are identified by the `_dlq_specs()` stable-ID set, which `_reset_dlq_state`
    re-baselines against. Environment-wide on purpose — a stray row in any tenant reaches a
    platform-admin-scoped agent — and safe because `_assert_not_production()` runs on both entry
    points. With `EVAL_EMPTY_DLQ_BASELINE=1` (commander ADR 0010) the fixture exclusion is dropped
    and every `dead_letter` row is swept; opt-in, because flipping the baseline breaks every
    `dlq_*` scenario written against the standing pool."""
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

    Two checks in `eval_safety.assert_safe_target()`: the `ENVIRONMENT=production` label (ADR
    0008) and, since WO-R2-18, the identity of the `database_url`/`redis_url` every `DELETE` here
    runs against. One gate, both entry points — `reset()` propagates the `RuntimeError`, `main()`
    turns it into stderr + `exit(1)`. No `allow_production` lever, by design."""
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
    """Programmatic entry point; returns a JSON-encodable summary dict.

    Raises `RuntimeError` before anything connects when `ENVIRONMENT=production`, or when
    `database_url`/`redis_url` are not the ones `settings` names (D-08, WO-R2-18);
    `allow_target_mismatch=True` overrides that."""
    _assert_not_production(
        database_url,
        redis_url,
        allow_target_mismatch=allow_target_mismatch,
    )

    # Local import: only callers of this reset pay the seeder's heavy DB imports.
    from scripts import seed_eval_fixtures  # type: ignore[import-not-found]

    engine = create_async_engine(database_url, echo=False)
    # Platform (cross-tenant) scope: this script sets no `app.tenant_id`, which ADR 0026
    # refuses, and runs as the non-owner `incident_app` role with no BYPASSRLS.
    factory = platform_session_factory(engine)
    redis = aioredis.from_url(redis_url, decode_responses=True)

    try:
        # First, not last: the purge can raise, and run last it raised after every destructive
        # step had committed (WO-R2-18). Free ordering — nothing here creates those records.
        idempotency_purged = 0
        if purge_idempotency:
            idempotency_purged = await _purge_idempotency_records(factory)
        chaos_cleared = await _clear_chaos_keys(redis)
        job_cache_cleared = await _clear_job_read_cache(redis)
        timers_cleared = await _clear_scheduled_replays(redis)
        pauses_cleared = await _clear_dag_pauses(redis)
        lag_samples_cleared = await _clear_lag_samples(redis)
        # Order-independent of the seed: the fixture alerts use non-chaos sources.
        chaos_alerts_resolved = await _resolve_chaos_alerts(factory)
        # Everything else active outside the five seeded alerts; spared by stable() id
        # (WO-R2-131).
        organic_alerts_resolved = await _resolve_organic_alerts(factory)
        seed_summary = await seed_eval_fixtures.seed(
            database_url=database_url,
            redis_url=redis_url,
            reset=True,
            # The seeder gates its own target too (WO-R2-19); thread the
            # override through so a deliberate mismatch cannot stop halfway.
            allow_target_mismatch=allow_target_mismatch,
        )
        # Chaos-owner users from any tenant `create_bad_data_job` ran against.
        chaos_owners_deleted = await _delete_chaos_owner_users(factory)
        # After the seed/reset, which restores replayed fixtures to `dead_letter`. Declared
        # fixtures go before the sweep, so they are deleted rather than `cancelled`.
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
        "lag_samples_cleared": lag_samples_cleared,
        "organic_alerts_resolved": organic_alerts_resolved,
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

    # Refuse on stderr + exit 1 before anything connects; `reset()` re-checks.
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
