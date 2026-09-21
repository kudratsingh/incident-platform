"""The platform pages on its own metric, once per episode (WO-R3-338, ADR 0039).

Before this, every alert the demo's agent triaged was synthesized by the scenario YAML: the
platform's own alert stream read the same three seeded fixtures before, during and after a
fault, so "jobs pile up, the platform pages, the agent responds" was true of the story and
not of the platform. Two rules now evaluate on the metrics loop's own clock.

Real rows on a module-local SQLite engine, the way `test_slo_evaluation.py` does it, so
committed alerts never leak into the shared `sqlite_engine`.
"""

from __future__ import annotations

import pathlib
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from app.config import Settings
from app.core.consumer_lag import LIVE_REFRESHED_GROUP, lag_key, samples_key
from app.models.alert import Alert
from app.models.audit import AuditLog
from app.models.base import Base
from app.models.enums import JobStatus, JobType, RemediationHint, UserRole
from app.models.job import Job
from app.models.tenant import DEFAULT_TENANT_ID, Tenant
from app.models.user import User
from app.services import alert_rules
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

_USER_ID = uuid.UUID("b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e")


class _RedisStub:
    """get/set only — the surface the lag reading and the metrics pass use."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self.store[key] = str(value)
        return True


def _settings(**overrides: Any) -> Settings:
    return Settings(environment="test", **overrides)


@pytest.fixture(autouse=True)
def _rules_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-on, stated in one place: every test here that wants them off says so."""
    monkeypatch.setattr(alert_rules, "get_settings", lambda: _settings())


@pytest_asyncio.fixture
async def factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        async with session.begin():
            session.add(
                Tenant(
                    id=DEFAULT_TENANT_ID,
                    slug="default",
                    name="Default Tenant",
                    is_active=True,
                )
            )
            session.add(
                User(
                    id=_USER_ID,
                    tenant_id=DEFAULT_TENANT_ID,
                    email="rules@example.com",
                    hashed_password="not-a-real-hash",
                    role=UserRole.USER,
                    is_active=True,
                )
            )
    try:
        yield sessions
    finally:
        await engine.dispose()


@pytest.fixture
def redis() -> _RedisStub:
    return _RedisStub()


def _write_window(redis: _RedisStub, lags: list[int], *, tick_seconds: int = 5) -> None:
    """A recorded window, newest first, exactly as `record_lag_sample` stores it."""
    import json

    now = datetime.now(UTC)
    samples = [
        {
            "lag": lag,
            "measured_at": (now - timedelta(seconds=tick_seconds * i)).isoformat(),
        }
        for i, lag in enumerate(lags)
    ]
    redis.store[samples_key(LIVE_REFRESHED_GROUP)] = json.dumps(samples)
    redis.store[lag_key(LIVE_REFRESHED_GROUP)] = str(lags[0])


async def _alerts(factory: async_sessionmaker[AsyncSession]) -> list[Alert]:
    async with factory() as session:
        return list(
            (await session.execute(select(Alert).order_by(Alert.fired_at)))
            .scalars()
            .all()
        )


async def _audit(factory: async_sessionmaker[AsyncSession]) -> list[AuditLog]:
    async with factory() as session:
        return list(
            (await session.execute(select(AuditLog).order_by(AuditLog.created_at)))
            .scalars()
            .all()
        )


async def _dead_letter(
    factory: async_sessionmaker[AsyncSession],
    count: int,
    *,
    hint: str | None,
    minutes_ago: float = 1.0,
    tenant_id: uuid.UUID = DEFAULT_TENANT_ID,
) -> None:
    created = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    async with factory() as session:
        async with session.begin():
            for _ in range(count):
                session.add(
                    Job(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        user_id=_USER_ID,
                        type=JobType.CSV_UPLOAD,
                        status=JobStatus.DEAD_LETTER,
                        payload={"rows": 1},
                        remediation_hint=hint,
                        created_at=created,
                        updated_at=created,
                        # What `list_dlq_messages` reports as `dead_lettered_at`, and
                        # what `JobSort.DEAD_LETTERED_AT` coalesces on.
                        completed_at=created,
                    )
                )


# ---------------------------------------------------------------------------
# consumer_stalled — the lag rule
# ---------------------------------------------------------------------------


async def test_a_lag_above_the_threshold_raises_one_alert_for_the_whole_episode(
    factory: async_sessionmaker[AsyncSession], redis: _RedisStub
) -> None:
    """The episode is the condition, not the tick. Six passes over a climbing lag page once."""
    for window in ([30], [80, 30], [400, 80, 30], [9000, 400, 80, 30]):
        _write_window(redis, window)
        await alert_rules.evaluate_alert_rules(factory, redis)

    alerts = await _alerts(factory)
    assert len(alerts) == 1, "a sustained breach must page once, not once a tick"
    alert = alerts[0]
    assert alert.severity == "critical"
    assert alert.source == alert_rules.CONSUMER_LAG_ALERT_SOURCE == "kafka:consumer_lag"
    assert alert.resolved_at is None
    assert alert.dedup_key == "rule:consumer_stalled:worker-dispatcher:0"

    payload = alert.extra_data or {}
    # The keys the commander's `AlertPayload` reads (`api/schemas.py`), so the poller can
    # hand this dict straight to the responder without stitching two levels together.
    assert payload["fingerprint"] == "consumer_stalled"
    assert payload["consumer_group"] == LIVE_REFRESHED_GROUP
    assert payload["group"] == LIVE_REFRESHED_GROUP
    assert payload["severity"] == "critical"
    assert payload["source"] == "kafka:consumer_lag"
    assert payload["lag"] == 30, "the lag that opened the episode, not the newest one"
    assert payload["threshold"] == 20
    assert isinstance(payload["measured_at"], str)
    assert LIVE_REFRESHED_GROUP in payload["summary"]


async def test_the_first_sample_below_the_threshold_resolves_the_episode(
    factory: async_sessionmaker[AsyncSession], redis: _RedisStub
) -> None:
    _write_window(redis, [9000])
    await alert_rules.evaluate_alert_rules(factory, redis)
    _write_window(redis, [0, 9000])
    outcome = await alert_rules.evaluate_alert_rules(factory, redis)

    assert len(outcome.resolved) == 1
    alerts = await _alerts(factory)
    assert len(alerts) == 1
    assert alerts[0].resolved_at is not None, "resolved, never deleted"


async def test_a_second_episode_raises_a_second_alert(
    factory: async_sessionmaker[AsyncSession], redis: _RedisStub
) -> None:
    """A dedup key that could not tell two episodes apart would page once, ever."""
    _write_window(redis, [9000])
    await alert_rules.evaluate_alert_rules(factory, redis)
    _write_window(redis, [0, 9000])
    await alert_rules.evaluate_alert_rules(factory, redis)
    _write_window(redis, [7000, 0, 9000])
    await alert_rules.evaluate_alert_rules(factory, redis)

    alerts = await _alerts(factory)
    assert [a.dedup_key for a in alerts] == [
        "rule:consumer_stalled:worker-dispatcher:0",
        "rule:consumer_stalled:worker-dispatcher:1",
    ]
    assert alerts[0].resolved_at is not None
    assert alerts[1].resolved_at is None


async def test_an_absent_reading_neither_raises_nor_resolves(
    factory: async_sessionmaker[AsyncSession], redis: _RedisStub
) -> None:
    """Unknown is not healthy (`get_consumer_lag`'s own rule). A window that stopped being
    written must not close an episode nobody measured out of."""
    _write_window(redis, [9000])
    await alert_rules.evaluate_alert_rules(factory, redis)

    redis.store.clear()
    outcome = await alert_rules.evaluate_alert_rules(factory, redis)

    assert outcome.raised == [] and outcome.resolved == []
    assert (await _alerts(factory))[0].resolved_at is None


async def test_a_recorded_constant_is_never_a_breach(
    factory: async_sessionmaker[AsyncSession], redis: _RedisStub
) -> None:
    """The seven `static` groups are seeded at 500–100,000 and nothing refreshes them, so a
    rule reading their value would page forever and never resolve. The rule reads MEASURED
    samples, which only the live group has."""
    redis.store[lag_key("shipping-consumer")] = "100000"
    redis.store[lag_key("analytics-consumer")] = "50000"

    outcome = await alert_rules.evaluate_alert_rules(factory, redis)

    assert outcome.raised == []
    assert await _alerts(factory) == []


# ---------------------------------------------------------------------------
# dlq_depth_warning — the depth rule
# ---------------------------------------------------------------------------


async def test_the_dlq_rule_fires_one_row_above_the_seeded_baseline(
    factory: async_sessionmaker[AsyncSession], redis: _RedisStub
) -> None:
    """Threshold 5 = the seeded baseline of 4 plus one, so the baseline world is quiet and
    the first row nobody seeded pages."""
    await _dead_letter(factory, 4, hint=RemediationHint.WAIT_AND_REPLAY.value)
    assert await alert_rules.evaluate_alert_rules(factory, redis) == alert_rules.RuleOutcome(
        raised=[], resolved=[]
    )

    await _dead_letter(factory, 1, hint=None, minutes_ago=0.1)
    await alert_rules.evaluate_alert_rules(factory, redis)

    alerts = await _alerts(factory)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.source == alert_rules.DLQ_DEPTH_ALERT_SOURCE == "dlq:threshold"
    assert alert.severity == "critical"
    assert alert.dedup_key == "rule:dlq_depth_warning:total:0"

    payload = alert.extra_data or {}
    assert payload["fingerprint"] == "dlq_depth_warning"
    assert payload["dlq_depth"] == 5
    assert payload["threshold"] == 5
    assert payload["severity"] == "critical"
    assert payload["source"] == "dlq:threshold"
    # The row above the baseline carries no category, so the alert names the unclassified
    # slice rather than inventing one (`AlertPayload.dlq_scope`, commander ADR 0032).
    assert payload["remediation_hint"] is None
    assert payload["dlq_scope"] == "unclassified"
    assert "5" in payload["summary"]


async def test_the_dlq_alert_names_the_category_that_pushed_it_over(
    factory: async_sessionmaker[AsyncSession], redis: _RedisStub
) -> None:
    await _dead_letter(factory, 4, hint=RemediationHint.HUMAN_REQUIRED.value)
    await _dead_letter(
        factory, 2, hint=RemediationHint.REPLAY_SAFE.value, minutes_ago=0.1
    )

    await alert_rules.evaluate_alert_rules(factory, redis)

    payload = (await _alerts(factory))[0].extra_data or {}
    assert payload["remediation_hint"] == "replay_safe"
    assert payload["dlq_scope"] is None


async def test_the_dlq_episode_resolves_when_the_queue_drains(
    factory: async_sessionmaker[AsyncSession], redis: _RedisStub
) -> None:
    await _dead_letter(factory, 6, hint=RemediationHint.REPLAY_SAFE.value)
    await alert_rules.evaluate_alert_rules(factory, redis)

    async with factory() as session:
        async with session.begin():
            rows = (
                (
                    await session.execute(
                        select(Job).where(Job.status == JobStatus.DEAD_LETTER).limit(2)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.status = JobStatus.COMPLETED

    outcome = await alert_rules.evaluate_alert_rules(factory, redis)

    assert len(outcome.resolved) == 1
    assert (await _alerts(factory))[0].resolved_at is not None


async def test_one_tenants_backlog_does_not_page_another(
    factory: async_sessionmaker[AsyncSession], redis: _RedisStub
) -> None:
    """Tenant-scoped, and the constraint that enforces the dedup is `(tenant_id, dedup_key)`."""
    other = uuid.uuid4()
    other_user = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            session.add(
                Tenant(id=other, slug="other", name="Other", is_active=True)
            )
            session.add(
                User(
                    id=other_user,
                    tenant_id=other,
                    email="other@example.com",
                    hashed_password="not-a-real-hash",
                    role=UserRole.USER,
                    is_active=True,
                )
            )
    await _dead_letter(factory, 6, hint=None)

    await alert_rules.evaluate_alert_rules(factory, redis)

    alerts = await _alerts(factory)
    assert [a.tenant_id for a in alerts] == [DEFAULT_TENANT_ID]


# ---------------------------------------------------------------------------
# What an operator, and the console, can see
# ---------------------------------------------------------------------------


async def test_every_raise_and_resolve_writes_one_audit_row(
    factory: async_sessionmaker[AsyncSession], redis: _RedisStub
) -> None:
    """The console draws its "platform paged" station from this stream, so the row carries
    the fingerprint and the sentence under it."""
    _write_window(redis, [9000])
    await alert_rules.evaluate_alert_rules(factory, redis)
    _write_window(redis, [0, 9000])
    await alert_rules.evaluate_alert_rules(factory, redis)

    rows = [r for r in await _audit(factory) if r.action.startswith("alert.")]
    assert [r.action for r in rows] == ["alert.raised", "alert.resolved"]
    raised = rows[0]
    assert raised.resource_type == "alert"
    assert raised.extra_data is not None
    assert raised.extra_data["fingerprint"] == "consumer_stalled"
    assert raised.extra_data["summary"]
    assert raised.resource_id == str((await _alerts(factory))[0].id)


async def test_the_rules_are_off_when_the_setting_says_so(
    factory: async_sessionmaker[AsyncSession],
    redis: _RedisStub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        alert_rules, "get_settings", lambda: _settings(alert_rules_enabled=False)
    )
    _write_window(redis, [9000])
    await _dead_letter(factory, 9, hint=None)

    outcome = await alert_rules.evaluate_alert_rules(factory, redis)

    assert outcome.raised == [] and outcome.resolved == []
    assert await _alerts(factory) == []


async def test_the_thresholds_are_settings(
    factory: async_sessionmaker[AsyncSession],
    redis: _RedisStub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        alert_rules,
        "get_settings",
        lambda: _settings(
            consumer_lag_alert_threshold=10_000, dlq_depth_alert_threshold=99
        ),
    )
    _write_window(redis, [9000])
    await _dead_letter(factory, 20, hint=None)

    assert (await alert_rules.evaluate_alert_rules(factory, redis)).raised == []


def test_the_ledger_note_is_written_where_a_re_pin_will_look() -> None:
    """Static tripwire, the same discipline `test_run_record_shape_delta.py` uses for
    WO-R3-328: the re-pin's note has to be readable off a file rather than reconstructed
    from a diff, and this order moves a tool description — so the reader has to be able to
    find the reason without asking anybody."""
    root = pathlib.Path(__file__).resolve().parents[3]
    claude_md = (root / "CLAUDE.md").read_text(encoding="utf-8")
    for token in (
        "WO-R3-338",
        "ADR 0039",
        "alert.raised",
        "alert.resolved",
        "METRICS_LOOP_INTERVAL_SECONDS",
    ):
        assert token in claude_md, token

    adr = root / "docs" / "ADR" / "0039-the-platform-pages-on-its-own-metric.md"
    assert adr.exists(), "ADR 0039 is missing"
    assert "0039-the-platform-pages-on-its-own-metric.md" in (
        root / "docs" / "ADR" / "README.md"
    ).read_text(encoding="utf-8"), "the ADR index has no row for 0039"

    env_example = (root / ".env.example").read_text(encoding="utf-8")
    for setting in (
        "METRICS_LOOP_INTERVAL_SECONDS",
        "ALERT_RULES_ENABLED",
        "CONSUMER_LAG_ALERT_THRESHOLD",
        "DLQ_DEPTH_ALERT_THRESHOLD",
    ):
        assert setting in env_example, setting


async def test_the_reset_can_close_every_open_episode(
    factory: async_sessionmaker[AsyncSession], redis: _RedisStub
) -> None:
    """What `reset_eval_state._resolve_rule_alerts` calls: the boundary closes an episode
    the same way a recovery does, so the audit stream has no open raise without a resolve."""
    _write_window(redis, [9000])
    await _dead_letter(factory, 9, hint=None)
    await alert_rules.evaluate_alert_rules(factory, redis)
    assert len(await _alerts(factory)) == 2

    closed = await alert_rules.resolve_open_episodes(factory, reason="world reset")

    assert len(closed) == 2
    assert all(a.resolved_at is not None for a in await _alerts(factory))
    resolutions = [
        r for r in await _audit(factory) if r.action == alert_rules.ALERT_RESOLVED_ACTION
    ]
    assert len(resolutions) == 2
    assert {r.extra_data["resolved_reason"] for r in resolutions if r.extra_data} == {
        "world reset"
    }
