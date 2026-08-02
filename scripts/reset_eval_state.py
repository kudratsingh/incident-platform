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
  3. **Idempotency records** — with `--purge-idempotency`, `DELETE`s
     every `idempotency_records` row for the seeded incident-commander
     service account. Off by default; the 24h TTL from [ADR 0010]
     handles the common case, but opt-in purge is useful when a
     scenario needs a guaranteed-fresh cache.

## Guardrails

- **Refuses to run in production.** Explicit `Settings.environment !=
  "production"` check at the top of `main()`. Same belt-and-braces
  reasoning as ADR 0008: two independent gates on the same invariant.
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
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

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

# Every Redis key namespace the chaos framework writes into. Anything
# added by a new chaos hook has to land here (or the reset will leak
# state across scenarios). Kept as a set so `SCAN`-style clearing is
# straightforward.
_CHAOS_KEY_PATTERNS = (
    "chaos:*",
    "kafka:consumer:*:killed",  # kill_key_for()
    "kafka:consumer:*:latency_ms",  # latency_key_for()
)


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


def _refuse_in_production() -> None:
    """Loud failure if invoked against a production platform. Backed by
    ADR 0008's environment gate — this is the belt on top of the braces."""
    env = get_settings().environment
    if env == "production":
        print(
            "ERROR: reset_eval_state.py refuses to run in production "
            f"(ENVIRONMENT={env!r}). If this is a real production-parity "
            "eval env, override ENVIRONMENT before invoking.",
            file=sys.stderr,
        )
        sys.exit(1)


async def reset(
    *,
    database_url: str = _DB_URL,
    redis_url: str = _REDIS_URL,
    purge_idempotency: bool = False,
) -> dict[str, Any]:
    """Programmatic entry point. Returns a summary dict suitable for
    JSON encoding by the CLI or the eval harness."""
    # Local import so the seed script's heavy DB imports are only paid
    # by scenarios that actually run this reset.
    from scripts import seed_eval_fixtures  # type: ignore[import-not-found]

    engine = create_async_engine(database_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    redis = aioredis.from_url(redis_url, decode_responses=True)

    try:
        chaos_cleared = await _clear_chaos_keys(redis)
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
        idempotency_purged = 0
        if purge_idempotency:
            idempotency_purged = await _purge_idempotency_records(factory)
    finally:
        await redis.aclose()
        await engine.dispose()

    return {
        "chaos_keys_cleared": chaos_cleared,
        "chaos_owners_deleted": chaos_owners_deleted,
        "dlq_reset": seed_summary["dlq_reset"],
        "idempotency_purged": idempotency_purged,
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
