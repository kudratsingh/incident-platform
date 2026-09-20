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
  3. **Tier-1 action residue** — delayed-replay timers on `jobs:dlq_replay_delayed` and any
     `dag:paused:*` flag. Each bleeds into the next scenario: a timer shrinks the DLQ unprompted,
     a stale pause holds a DAG in WAITING (ADR 0011). `pause_dag_chaos` (WO-R3-275, ADR 0029)
     writes the same `dag:paused:*` key the agent's `pause_dag` does, so `dag_pauses_cleared`
     counts both and a leftover pause is no longer evidence the agent acted.

     **Not the recent-lag window, since WO-R3-333** (ADR 0038). It used to be cleared here as
     residue; it is history, and clearing it opened the demo's lag chart on two points while the
     fault it was drawn to show was climbing 0 → 10 → 30. `lag_samples_cleared` stays in the
     summary as a permanent 0 — see `_LAG_SAMPLES_CLEARED` for why the counter stays at all.
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
  8. **The stale-cache fixture key** — `cache:jobs:worker-dispatcher:hot_set`, restored with the
     Redis sweeps above and reported as `hot_set_reseeded` (**WO-R3-310**). The seed writes it
     too, but nothing said so in the summary, so an evicted key was invisible until a later
     world read `exists: false` — 108 unledgered fixture values, recovered by hand. It is also
     the one fixture written with a TTL, which is exactly what a `volatile-*` eviction policy
     takes first, so it is the one most likely to be gone. Read before the seed, so the count
     reports the world the reset *found*.
  9. **Circuit breakers** — `breaker:state:*` back to closed with the failure fields null, plus
     the `breaker:reset:at` signal that makes the worker's in-memory registry forget
     (`breaker_state.reset_breaker_states`, reported as `breakers_reset`; **WO-R3-311**, ADR
     0036). Outside `chaos:*` and therefore not swept by step 1, so one `degrade_downstream`
     used to contaminate every later reading for the record's 24 h TTL. Deleting the key is not
     the fix twice over: the registry writes the same failure back, and an absent record reads
     as *unknown* rather than closed (ADR 0030).
 10. **Open agent runs** — every `agent_runs` row with no `finished_at` is closed as `failed`
     with a `closed_by: reset` entry appended to `phase_history` (reported as
     `agent_runs_closed`; **WO-R3-315**, the gap ADR 0035 recorded). Only the responder reports
     a terminal state, and this reset is what just took its world away, so without this a run
     the reset ended stays open for ever. Closed, never deleted: the rows are what a console
     replays, and the responder's own words (`briefing`, `current_hypothesis`, `last_step`) are
     left untouched.
 11. **The boundary** — one `lab.world_reset` audit row, appended last, carrying every counter
     above as its payload (reported as `world_reset_recorded`; **WO-R3-327**). The only step
     here that restores nothing: it says *when* the take ended. `audit_logs` is append-only, so
     after a reset the newest `chaos.*` row was still the previous take's kill and the `/demo`
     page opened a clean world at `agent remediating`, clock counting from an incident that no
     longer existed. The console now reads rows and runs newer than this row and nothing older.
     Its own prefix, because the newest `chaos.*` row IS the fault to that console; withheld
     from the agent beside `chaos.` under the same `chaos:invoke` condition, because the
     payload is the mechanism list (ADR 0012's 2026-09-20 amendment). See
     `_record_world_reset` for the principal and why it raises rather than degrades.

What it deliberately does **not** touch, so nobody adds a step for it: `pool:state:*`
(**WO-R3-289**, ADR 0033). Like `breaker:state:*` it is a platform namespace outside `chaos:*`, but
unlike a breaker it is not state a scenario set — it is a live description of each process's
connection pool, rewritten by that process every ten seconds under a 60 s TTL. Clearing it would
only create a window in which the platform could say nothing about its own pools, and the reading
already answers an absent record as *unknown with a reason* rather than as a healthy pool.

## Guardrails

- **Refuses to run against a target it was not configured for.** `_assert_not_production()`
  delegates to `eval_safety.assert_safe_target()`: the `ENVIRONMENT=production` label, and that
  `database_url`/`redis_url` are the ones `settings` names. The label alone was the bug
  (WO-R2-18) — it inspects the local process while every `DELETE` runs against the DSN the caller
  passed. Pass `--i-know-what-im-doing` (CLI) or `allow_target_mismatch=True` (library).
- Enforced on *both* entry points: `main()` turns a refusal into stderr + `exit(1)`, `reset()`
  re-raises it before any engine or Redis client exists. Gating only the CLI was D-08.
- **Audit rows are ground truth, and this script only ever appends to them.** It writes exactly
  one row — step 11's `lab.world_reset` boundary — and never updates or deletes any. The job and
  user DELETEs have one documented side effect: the FKs are `ON DELETE SET NULL`, so
  `audit_logs.job_id` / `audit_logs.user_id` go NULL and `job_triages` CASCADEs with its job.
  `resource_id` survives, so it — not the FK columns — is the durable join key for audit-based
  grading (ADR 0012).
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
from datetime import UTC, datetime
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
# `chaos:kill:{group}`, `chaos:latency:{group}`, `chaos:pause:{loop}`, `chaos:db_pool:hold`,
# `chaos:downstream:bulk_api_sync`, `chaos:db_query:slow`. A new hook adds no pattern here, and
# `test_every_chaos_key_helper_lives_under_the_chaos_namespace` fails if one escapes. Deleting
# `chaos:db_pool:hold` is also what releases the held DB connections: the worker's holder gives them
# back on its next pass when the key is gone, TTL or no TTL (ADR 0031). `chaos:db_query:slow` is the
# one key whose effect outlives the sweep at all, and only by the query already in flight — at most
# `query_ms`, 10 s at its ceiling (ADR 0034).
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

# The reset touches NEITHER consumer-lag key, and the counter stays to say so (WO-R3-333,
# ADR 0038). It is reported rather than dropped for two reasons: `make eval-reset` and the
# `lab.world_reset` boundary row both carry this summary, so a key that disappears reads as a
# step that stopped being reported rather than one that stopped being needed — and the number
# is now a claim worth making, that this reset preserved the window it used to delete.
#
# WO-R3-254 cleared the window as residue, on the reading that a lag trend from the previous
# run is the first thing the next run sees. The demo's third live take (2026-09-20) showed the
# cost: the operator opens the page on a freshly reset world, and the chart that is supposed
# to show the fault climbing has two points in a fifteen-minute window. The window is history,
# under a TTL deliberately longer than the value key's (ADR 0037) precisely so it outlives the
# pass that wrote it; deleting it on the boundary contradicted the reason it is kept.
#
# The lag VALUE key is untouched as it always was, here and in the seeder: the metrics loop
# owns it under a 90 s TTL, so it is already fresh-or-absent — which is what `check_backpressure`
# needs and what makes deleting it pointless as well as blinding.
_LAG_SAMPLES_CLEARED = 0

# What the reset writes into a run it closes itself, so an operator reading the console can
# tell a responder that gave up from a world that was taken away under it (WO-R3-315).
AGENT_RUN_CLOSED_BY = "reset"

# Who the boundary row says performed the reset (WO-R3-327). A literal mirror of
# `scripts/seed_incident_commander.py::_CHAOS_SA_NAME`, read from the same envvar, because
# a script must not import another script's module just to learn a name. The account is
# looked up rather than required: an absent one costs the row its attribution, never the
# row. `_record_world_reset` explains why this principal and not another.
_EVALUATOR_SA_NAME = os.getenv("SA_CHAOS_NAME", "incident-commander-chaos")


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


async def _clear_dag_pauses(redis: aioredis.Redis) -> int:
    """Delete every `dag:paused:*` flag: since ADR 0011 the resolver enforces it, so a pause
    left by one scenario holds the next one's DAG in WAITING."""
    return await _scan_delete(redis, "dag:paused:*")


async def _reseed_hot_set(redis: aioredis.Redis) -> int:
    """Restore `cache:jobs:worker-dispatcher:hot_set`, and say whether it had to be written.

    `remediate_stale_cache_success` opens on this key, and `saturate_redis` evicts it — it is
    the one fixture written with a TTL, so a `volatile-*` policy takes it first, and a stack up
    longer than a day loses it to the TTL itself. Every later world then reads `exists: false`,
    which is a world nobody graded (**WO-R3-310**).

    The seed writes the same key a few lines further on. This step exists anyway, for the reason
    the finding was filed: the summary said nothing either way, so nobody could tell an intact
    world from a restored one, and the commander's Makefile comment claimed a re-seed that
    nothing here asserted. Returns 1 when the key was absent or held something other than the
    seeded payload — compared against `seed_eval_fixtures.hot_set_payload()` rather than a
    restated id set (D-14) — and 0 on a world that was already right.
    """
    from scripts import seed_eval_fixtures  # type: ignore[import-not-found]

    expected = seed_eval_fixtures.hot_set_payload()
    found = await redis.get(seed_eval_fixtures._HOT_SET_KEY)
    await seed_eval_fixtures._seed_hot_set(redis)
    return 0 if found == expected else 1


async def _reset_breaker_states(redis: aioredis.Redis) -> int:
    """Close every published circuit breaker and tell the registries that own them.

    The step's whole content is in `app/core/breaker_state.reset_breaker_states` — the record
    shape and the signal belong beside the code that writes them — and it is called from here
    because `breaker:state:*` is a platform namespace outside `chaos:*` (ADR 0030), so step 1's
    sweep never carried it and one `degrade_downstream` contaminated every later recording for
    24 h (**WO-R3-311**). Runs after that sweep, so the fault is gone before the breaker that
    the fault opened is closed. Returns how many records were rewritten.
    """
    from app.core.breaker_state import reset_breaker_states

    return await reset_breaker_states(redis)


async def _close_open_agent_runs(session_factory: Any) -> int:
    """Close every `agent_runs` row this reset leaves open, as `failed`. **WO-R3-315.**

    ADR 0035 recorded the gap rather than solving it: a run is closed only by the responder
    reporting a terminal state, so a run this reset ends — and ending it is exactly what a reset
    does — stays open for ever, and the rows accumulate across every scenario.

    `failed`, not `resolved` or `escalated`: those two are claims about what the responder
    concluded, and it concluded nothing. The marker goes in `phase_history`, which is the row's
    own timeline and already append-only, so no column is added and a console draws "failed,
    closed by reset" from what is there. Never DELETEd — the rows are what the demo replays, and
    the responder's own words (`briefing`, `current_hypothesis`, `last_step`, `scenario`) are
    left byte-identical. A run still reporting will get `agent_run_already_finished` on its next
    call, which is the truth: its world is gone. The reporter is fail-open, so it logs and
    carries on.

    Idempotent — a second run finds nothing open. Environment-wide, like every other step here,
    and safe for the same reason (`_assert_not_production()` on both entry points).
    """
    from app.models.agent_run import AgentRun  # type: ignore[import-not-found]
    from app.models.enums import AgentRunState
    from sqlalchemy import select

    closed_at = datetime.now(UTC)
    closed = 0
    async with session_factory() as session:
        async with session.begin():
            open_runs = (
                (
                    await session.execute(
                        select(AgentRun).where(AgentRun.finished_at.is_(None))
                    )
                )
                .scalars()
                .all()
            )
            for run in open_runs:
                # Reassigned rather than appended in place: a JSON column only travels if the
                # attribute is set (the same trap `PortableJSON` has everywhere else).
                run.phase_history = [
                    *(run.phase_history or []),
                    {
                        "state": AgentRunState.FAILED.value,
                        "at": closed_at.isoformat(),
                        "closed_by": AGENT_RUN_CLOSED_BY,
                    },
                ]
                run.state = AgentRunState.FAILED.value
                run.finished_at = closed_at
                closed += 1
    return closed


async def _record_world_reset(session_factory: Any, counters: dict[str, Any]) -> int:
    """Append the one `lab.world_reset` audit row that closes this take. **WO-R3-327.**

    Every other step here restores a world. This one says *when* — and it exists because
    nothing else did. The `/demo` console derives its phase strip from the newest
    `chaos.*` audit row, and audit rows are append-only by design, so after a reset the
    newest one was still the previous take's kill: a freshly wiped world opened the page
    at `agent remediating`, with a fault clock counting from an incident that no longer
    existed. The console cannot infer the boundary — it is not in the audit log, the
    Redis keys are gone, and `agent_runs` says only that a run ended — so the reset has
    to state it.

    **A row rather than a counter.** The boundary must survive in the same append-only
    place the rows it bounds live, must carry the platform's own clock (the console
    compares it against `created_at` on other rows), and must be readable by an operator
    who opened the page after the reset. That is an audit row and nothing else is.

    **Its own `lab.` prefix, not `chaos.`.** The console reads the newest `chaos.*` row
    as the moment the fault was injected. A boundary filed under that prefix would be
    read as a fault — exactly the reading it exists to remove. `lab.` is the lab talking
    about its own apparatus rather than about the world.

    **Withheld from the agent** by `hidden_audit_action_prefixes`, beside `chaos.` and
    under the same condition (ADR 0012's 2026-09-20 amendment). The payload is this
    dict: `chaos_keys_cleared`, `seeded_dlq_deleted`, `hot_set_reseeded` — the mechanism
    list, in the agent's own read surface, would be a stronger leak than any hook name.
    Human operators read it over REST and see everything, as they always have.

    **The principal is the evaluator's service account** (`SA_CHAOS_NAME`, default
    `incident-commander-chaos`), looked up in the seed tenant so a second tenant holding
    a copy cannot raise from the tail of the reset (the WO-R2-18 shape). Two reasons for
    that account and not a platform identity: it is the principal that fires every
    `chaos.*` row in the same timeline, so an operator sees one actor for the whole lab
    rather than two; and it is the only principal the withholding rule lets read this
    row back, so the row's author and its one machine reader are the same identity. When
    the account is absent — a stack seeded for fixtures but not for the agent — the row
    is still written with a null `principal_id`, because a boundary nobody signed is
    worth more than no boundary at all.

    Written LAST, after every other step, for two reasons: the payload is the summary of
    what those steps did, and a boundary stamped before the seed finished would put the
    restored fixtures on the far side of it.

    Not idempotent, and must not be: one row per reset is the point. Two resets are two
    boundaries and the console reads the newest. Returns 1 — the count the summary
    carries, because this is the one step whose own row cannot report on itself.
    """
    from app.models.service_account import ServiceAccount
    from app.repositories.audit import AuditRepository
    from app.services.operator_audit import record_world_reset
    from sqlalchemy import select

    from scripts import seed_eval_fixtures  # type: ignore[import-not-found]

    async with session_factory() as session:
        async with session.begin():
            tenant = await seed_eval_fixtures._ensure_tenant(
                session, seed_eval_fixtures._TENANT_SLUG
            )
            found = (
                (
                    await session.execute(
                        select(ServiceAccount.id).where(
                            ServiceAccount.name == _EVALUATOR_SA_NAME,
                            ServiceAccount.tenant_id == tenant.id,
                        )
                    )
                )
                .scalars()
                .first()
            )
            await record_world_reset(
                AuditRepository(session),
                tenant_id=uuid.UUID(str(tenant.id)),
                principal_id=None if found is None else uuid.UUID(str(found)),
                counters=counters,
            )
    return 1


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
        # Before the seed's own write, so the count reports the world this reset found
        # (WO-R3-310) rather than the one it leaves.
        hot_set_reseeded = await _reseed_hot_set(redis)
        # After the chaos sweep above: the fault goes first, then the breaker it opened
        # (WO-R3-311).
        breakers_reset = await _reset_breaker_states(redis)
        # Order-independent of the seed: the fixture alerts use non-chaos sources.
        chaos_alerts_resolved = await _resolve_chaos_alerts(factory)
        # Everything else active outside the five seeded alerts; spared by stable() id
        # (WO-R2-131).
        organic_alerts_resolved = await _resolve_organic_alerts(factory)
        # Beside the alert sweep, because it is the same kind of step: close out what the
        # last run left open (WO-R3-315).
        agent_runs_closed = await _close_open_agent_runs(factory)
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
        # Last of the restoring steps: projects the rows as every step above finally
        # left them.
        read_model_keys = await _rebuild_read_model(factory, redis)
        # Built here rather than at the `return` because this dict IS the boundary row's
        # payload as well as the caller's summary — one literal, so the two cannot drift.
        counters: dict[str, Any] = {
            "agent_runs_closed": agent_runs_closed,
            "breakers_reset": breakers_reset,
            "chaos_alerts_resolved": chaos_alerts_resolved,
            "chaos_keys_cleared": chaos_cleared,
            "chaos_owners_deleted": chaos_owners_deleted,
            "dag_pauses_cleared": pauses_cleared,
            "dlq_reset": seed_summary["dlq_reset"],
            "dlq_swept": dlq_swept,
            "timestamps_rebaselined": seed_summary["timestamps_rebaselined"],
            "empty_dlq_baseline": _empty_dlq_baseline(),
            "hot_set_reseeded": hot_set_reseeded,
            "job_cache_cleared": job_cache_cleared,
            # Always 0, on purpose, and reported anyway — `_LAG_SAMPLES_CLEARED`.
            "lag_samples_cleared": _LAG_SAMPLES_CLEARED,
            "organic_alerts_resolved": organic_alerts_resolved,
            "read_model_keys_rebuilt": read_model_keys,
            "seeded_dlq_deleted": seeded_dlq_deleted,
            "idempotency_purged": idempotency_purged,
            "timers_cleared": timers_cleared,
        }
        # Truly last: the one row that says the take above is over (WO-R3-327). It
        # raises rather than degrading, because a reset the console cannot see is a
        # reset that leaves the page reading the previous take's fault as current.
        world_reset_recorded = await _record_world_reset(factory, counters)
    finally:
        await redis.aclose()
        await engine.dispose()

    return {
        **counters,
        # The one count the row cannot carry about itself.
        "world_reset_recorded": world_reset_recorded,
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
