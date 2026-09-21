"""The platform's own alert rules on a real Postgres (WO-R3-338, ADR 0039).

One episode, end to end, on the server the demo runs against: the consumer is killed, the
recorded lag samples climb, the platform pages ONCE across many passes, the group is
restarted, the backlog drains and the episode resolves — with an `alert.raised` and an
`alert.resolved` row an operator console can draw a "paged" station from.

Three claims this tier exists for, none of which the SQLite unit tier can make:

**One alert across replicas.** `worker_loop` runs in every replica and each one evaluates
the same tick. The de-duplication is the unique constraint on `(tenant_id, dedup_key)`, and
what makes it safe is Postgres's behaviour under a concurrent insert of the same key — the
second transaction BLOCKS on the first's uncommitted row and then fails, where SQLite's
serialised writers never meet. Two evaluations are run concurrently here and exactly one
alert must exist afterwards.

**The writes land under the scope every platform writer declares.** `alerts` and
`audit_logs` are FORCE-RLS tables (ADR 0015/0026), so a rule writing from the worker's
session factory has to declare `app.tenant_scope = 'platform'` or be refused. On SQLite
there are no policies to satisfy, so a rule that could never write in production would pass.

**`extra_data` is JSONB here and plain JSON there.** The alert payload is what the eval
runner will take verbatim (WO-R3-339), so it is read back from the server rather than from
the identity map.

Red before the change: `app.services.alert_rules` did not exist, and the metrics pass wrote
no alert at any lag.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from app.core.consumer_lag import LIVE_REFRESHED_GROUP, lag_key, samples_key
from app.models.alert import Alert
from app.models.audit import AuditLog
from app.models.enums import JobStatus, JobType, RemediationHint, UserRole
from app.models.job import Job
from app.models.tenant import Tenant
from app.models.user import User
from app.services import alert_rules
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

try:
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    _HAS_TC = True
except Exception:  # pragma: no cover
    _HAS_TC = False


def _has_docker() -> bool:
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=True)
        return True
    except Exception:  # pragma: no cover - environment-dependent
        return False


pytestmark = pytest.mark.skipif(
    not _HAS_TC or not _has_docker(),
    reason="needs Docker + testcontainers[postgres]",
)


class _RedisStub:
    """The lag window lives in Redis and this tier is about Postgres, so the cache is a
    stub rather than a second container: every claim below is about rows."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self.store[key] = str(value)
        return True

    def record_window(self, lags: list[int], *, tick_seconds: int = 5) -> None:
        """A window as `record_lag_sample` stores it: newest first, each sample dated."""
        now = datetime.now(UTC)
        self.store[samples_key(LIVE_REFRESHED_GROUP)] = json.dumps(
            [
                {
                    "lag": lag,
                    "measured_at": (
                        now - timedelta(seconds=tick_seconds * i)
                    ).isoformat(),
                }
                for i, lag in enumerate(lags)
            ]
        )
        self.store[lag_key(LIVE_REFRESHED_GROUP)] = str(lags[0])


@pytest.fixture(scope="module")
def pg() -> Any:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


@pytest.fixture(scope="module")
def migrated_url(pg: Any) -> str:
    url = str(pg.get_connection_url())
    _alembic(url, "upgrade", "head")
    return url


def _alembic(database_url: str, *args: str) -> None:
    """`ALEMBIC_DATABASE_URL` is popped, not overridden: `env.py::_get_url` prefers it
    (ADR 0015), so an inherited value would migrate somewhere else entirely."""
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env.pop("ALEMBIC_DATABASE_URL", None)
    subprocess.check_call(
        [sys.executable, "-m", "alembic", "-c", str(REPO_ROOT / "alembic.ini"), *args],
        env=env,
        cwd=REPO_ROOT,
    )


@pytest_asyncio.fixture
async def engine(migrated_url: str) -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine(migrated_url, echo=False)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def factory(
    engine: AsyncEngine,
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """The worker's own shape: a platform-scoped factory, which is what ADR 0026 requires
    of a writer that spans tenants. Declared with an `after_begin` hook on the sync session
    class exactly as `core/tenant_scope.platform_session_factory` builds it."""
    from app.core.tenant_scope import platform_session_factory

    sessions = platform_session_factory(engine)
    async with sessions() as session:
        async with session.begin():
            # `audit_logs` is deliberately NOT cleared: its RESTRICTIVE
            # `audit_logs_block_delete` policy makes a DELETE a silent no-op (ADR 0015),
            # and a fixture that pretended otherwise would be asserting against rows an
            # earlier test in this module wrote. Every audit assertion below is scoped to
            # the alert ids the test itself produced.
            await session.execute(text("DELETE FROM alerts"))
            await session.execute(text("DELETE FROM jobs"))
    yield sessions


@pytest_asyncio.fixture
async def tenant_id(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """The platform tenant the lag rule pins its alert to, plus a user for the DLQ rows."""
    from app.models.tenant import DEFAULT_TENANT_ID

    async with factory() as session:
        async with session.begin():
            existing = (
                await session.execute(
                    select(Tenant).where(Tenant.id == DEFAULT_TENANT_ID)
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    Tenant(
                        id=DEFAULT_TENANT_ID,
                        slug="default",
                        name="Default Tenant",
                        is_active=True,
                    )
                )
    return DEFAULT_TENANT_ID


@pytest_asyncio.fixture
async def user_id(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> uuid.UUID:
    async with factory() as session:
        async with session.begin():
            user = User(
                tenant_id=tenant_id,
                email=f"rules-{uuid.uuid4().hex[:8]}@example.com",
                hashed_password="not-a-real-hash",
                role=UserRole.USER,
                is_active=True,
            )
            session.add(user)
            await session.flush()
            return uuid.UUID(str(user.id))


async def _alerts(factory: async_sessionmaker[AsyncSession]) -> list[Alert]:
    async with factory() as session:
        return list(
            (await session.execute(select(Alert).order_by(Alert.fired_at)))
            .scalars()
            .all()
        )


async def _alert_actions(
    factory: async_sessionmaker[AsyncSession], *alert_ids: uuid.UUID
) -> list[str]:
    """The transitions recorded for these alerts, oldest first.

    Scoped by `resource_id` rather than by action alone: the audit trail is append-only at
    the database level, so rows from an earlier test in this module are still there.
    """
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.action.like("alert.%"),
                        AuditLog.resource_id.in_([str(i) for i in alert_ids]),
                    )
                    .order_by(AuditLog.created_at, AuditLog.id)
                )
            )
            .scalars()
            .all()
        )
        return [row.action for row in rows]


async def test_one_episode_from_the_kill_to_the_drain(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    redis = _RedisStub()

    # The consumer stops: the group keeps its assignment, so the recorded samples climb.
    for window in ([0], [6, 0], [24, 6, 0], [310, 24, 6, 0], [4100, 310, 24, 6, 0]):
        redis.record_window(window)
        await alert_rules.evaluate_alert_rules(factory, redis)

    alerts = await _alerts(factory)
    assert len(alerts) == 1, "a climbing lag is one incident, not one per pass"
    raised = alerts[0]
    assert raised.resolved_at is None
    assert raised.source == alert_rules.CONSUMER_LAG_ALERT_SOURCE

    # Read back from the server, not the identity map: this payload is what the eval
    # runner takes verbatim.
    payload = raised.extra_data or {}
    assert payload["fingerprint"] == alert_rules.CONSUMER_STALLED_FINGERPRINT
    assert payload["consumer_group"] == payload["group"] == LIVE_REFRESHED_GROUP
    assert payload["lag"] == 24, "the sample that first crossed the threshold"
    assert payload["threshold"] == 20

    # The group is restarted and the backlog drains.
    redis.record_window([0, 900, 4100, 310, 24])
    outcome = await alert_rules.evaluate_alert_rules(factory, redis)

    assert len(outcome.resolved) == 1
    assert (await _alerts(factory))[0].resolved_at is not None
    assert await _alert_actions(factory, raised.id) == [
        "alert.raised",
        "alert.resolved",
    ]


async def test_two_replicas_on_the_same_tick_raise_one_alert(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """The claim the constraint exists for. Both evaluations see no open episode, both
    compute ordinal 0, both insert `…:0` — and Postgres lets exactly one commit."""
    redis = _RedisStub()
    redis.record_window([5000])

    await asyncio.gather(
        alert_rules.evaluate_alert_rules(factory, redis),
        alert_rules.evaluate_alert_rules(factory, redis),
    )

    alerts = await _alerts(factory)
    assert len(alerts) == 1, "two replicas paged twice for one condition"
    assert await _alert_actions(factory, alerts[0].id) == ["alert.raised"], (
        "a suppressed alert must not leave an audit row behind"
    )


async def test_a_second_episode_pages_again_after_a_recovery(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    redis = _RedisStub()
    redis.record_window([900])
    await alert_rules.evaluate_alert_rules(factory, redis)
    redis.record_window([0, 900])
    await alert_rules.evaluate_alert_rules(factory, redis)
    redis.record_window([700, 0, 900])
    await alert_rules.evaluate_alert_rules(factory, redis)

    alerts = await _alerts(factory)
    assert [a.dedup_key for a in alerts] == [
        alert_rules.dedup_key(
            alert_rules.CONSUMER_STALLED_FINGERPRINT, LIVE_REFRESHED_GROUP, 0
        ),
        alert_rules.dedup_key(
            alert_rules.CONSUMER_STALLED_FINGERPRINT, LIVE_REFRESHED_GROUP, 1
        ),
    ]
    assert await _alert_actions(factory, alerts[0].id, alerts[1].id) == [
        "alert.raised",
        "alert.resolved",
        "alert.raised",
    ]


async def test_the_dlq_rule_pages_on_the_row_above_the_baseline(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Four seeded dead-letters is the baseline and stays quiet; the fifth pages, and the
    alert names the slice those rows are in."""
    redis = _RedisStub()
    await _dead_letter(factory, tenant_id, user_id, 4, RemediationHint.WAIT_AND_REPLAY)
    await alert_rules.evaluate_alert_rules(factory, redis)
    assert await _alerts(factory) == []

    await _dead_letter(factory, tenant_id, user_id, 1, None, seconds_ago=1)
    await alert_rules.evaluate_alert_rules(factory, redis)

    alerts = await _alerts(factory)
    assert len(alerts) == 1
    payload = alerts[0].extra_data or {}
    assert payload["fingerprint"] == alert_rules.DLQ_DEPTH_FINGERPRINT
    assert payload["dlq_depth"] == 5
    assert payload["remediation_hint"] is None
    assert payload["dlq_scope"] == "unclassified"

    # …and the reset closes it the way a recovery would, with its own row.
    closed = await alert_rules.resolve_open_episodes(factory, reason="world reset")
    assert len(closed) == 1
    assert await _alert_actions(factory, alerts[0].id) == [
        "alert.raised",
        "alert.resolved",
    ]


async def test_the_audit_row_names_no_principal_and_still_lands(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """No human and no service account raised this — the platform's own loop did. The row
    has to land anyway, under the platform scope, on a FORCE-RLS table."""
    redis = _RedisStub()
    redis.record_window([120])

    await alert_rules.evaluate_alert_rules(factory, redis)
    alert_id = (await _alerts(factory))[0].id

    async with factory() as session:
        row = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.action == alert_rules.ALERT_RAISED_ACTION,
                        AuditLog.resource_id == str(alert_id),
                    )
                )
            )
            .scalars()
            .one()
        )
    assert row.principal_type == "service_account"
    assert row.principal_id is None
    assert row.user_id is None
    assert row.resource_type == alert_rules.ALERT_RESOURCE_TYPE
    assert row.extra_data is not None
    assert row.extra_data["fingerprint"] == alert_rules.CONSUMER_STALLED_FINGERPRINT
    assert row.extra_data["summary"]


async def _dead_letter(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    count: int,
    hint: RemediationHint | None,
    *,
    seconds_ago: float = 300.0,
) -> None:
    at = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    async with factory() as session:
        async with session.begin():
            for _ in range(count):
                session.add(
                    Job(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        user_id=user_id,
                        type=JobType.CSV_UPLOAD,
                        status=JobStatus.DEAD_LETTER,
                        payload={"rows": 1},
                        remediation_hint=hint.value if hint is not None else None,
                        created_at=at,
                        updated_at=at,
                        completed_at=at,
                    )
                )
