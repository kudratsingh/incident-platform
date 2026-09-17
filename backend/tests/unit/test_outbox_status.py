"""`get_outbox_status` — the reading that makes a delivery stall legible.

Family B's two faults look identical from the top ("jobs accepted, nothing
executing") and have opposite evidence: a stopped consumer leaves the backlog
in Kafka, so consumer lag climbs; a stopped outbox relay leaves the backlog in
Postgres, so `outbox_events` rows pile up unpublished and age while lag stays
flat. Nothing the agent could call showed the second half, so the family had no
discriminating evidence at all (WO-R3-201 / plan 01 §7.1).

What these tests hold, and why each one is here rather than being obvious:

  * **A healthy outbox reads healthy.** The lesson of WO-R3-254 and WO-R3-267 is
    that a sensor which only looks right when something is wrong is a trap: the
    empty queue has to come back as a zero count with null ages, not as a crash
    and not as a suspicious-looking null count.
  * **Unknown is null with the reason, never a fabricated zero.** A relay
    heartbeat the platform has no record of is reported as unknown. A
    fabricated `0` would read as a relay that had just ticked, which is the one
    wrong answer this field can give.
  * **An abandoned row is not a delivery.** `mark_failed` stamps `published_at`
    as well as `failed_at` (ADR 0001 Decision item 3), so a naive
    `max(published_at)` reports a row the relay gave up on as a successful
    publish — i.e. it makes a fully stalled relay look like it published a
    moment ago. That is the exact shape of the defect the repo's fourth
    description rule was written about, so it gets its own test.
  * **The reading carries its own clock.** Every age is the database's clock at
    the moment of the reading minus the timestamp beside it, so a caller cannot
    be handed an age and a timestamp that disagree.
  * **The contract delta is pinned.** One new tool, one field list, no class
    docstring anywhere near the wire (plat #210: Pydantic copies a class
    docstring — on a model *or* on an enum used as a field type — straight into
    the schema the commander pins).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import app.mcp.tools  # noqa: F401  — import for @tool registration side effects
import pytest
from app.core.outbox_heartbeat import (
    RELAY_TICK_KEY,
    RELAY_TICK_TTL_SECONDS,
    TICK_UNKNOWN_NO_RECORD,
    TICK_UNKNOWN_UNREACHABLE,
    TICK_UNKNOWN_UNREADABLE,
    read_relay_tick,
    record_relay_tick,
)
from app.core.scopes import Scope
from app.dependencies import Principal
from app.mcp.registry import ToolContext, list_tools
from app.mcp.tools.outbox_status import (
    RELAY_TICK_INTERVAL_SECONDS,
    GetOutboxStatusInput,
    GetOutboxStatusOutput,
    get_outbox_status,
)
from app.models.outbox import OutboxEvent
from app.models.tenant import Tenant
from app.repositories.outbox import OutboxRepository
from sqlalchemy.ext.asyncio import AsyncSession

#: The whole read tier after this tool lands. Spelled out rather than counted
#: so the rebless note the coordinator writes can be read straight off a test,
#: and so a second new read tool cannot ride in on this one's count.
READ_TIER_AFTER = [
    "get_cache_key_info",
    "get_consumer_lag",
    "get_dag_state",
    "get_deploy_history",
    "get_incident",
    "get_outbox_status",
    "get_postgres_health",
    "get_redis_health",
    "get_trace",
    "list_active_alerts",
    "list_audit_events",
    "list_dlq_messages",
    "list_incidents",
    "search_traces",
]

#: Every field of the response, which is the other half of the contract delta.
OUTPUT_FIELDS = {
    "measured_at",
    "unpublished_count",
    "oldest_unpublished_at",
    "oldest_unpublished_age_s",
    "newest_unpublished_at",
    "newest_unpublished_age_s",
    "unpublished_past_attempt_limit",
    "last_publish_at",
    "seconds_since_last_publish",
    "relay_last_tick_at",
    "relay_heartbeat_age_s",
    "relay_heartbeat_known",
    "relay_heartbeat_unknown_reason",
    "relay_tick_interval_s",
}


class _RedisStub:
    """`get`/`set` only — the whole surface both sides of the heartbeat use."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.store: dict[str, str] = dict(values or {})
        self.ttls: dict[str, int | None] = {}
        self.raise_on_get = False
        self.raise_on_set = False

    async def get(self, key: str) -> str | None:
        if self.raise_on_get:
            raise ConnectionError("redis unreachable")
        return self.store.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        if self.raise_on_set:
            raise ConnectionError("redis unreachable")
        self.store[key] = str(value)
        self.ttls[key] = ex
        return True


def _ctx(db: AsyncSession, redis: Any, tenant_id: uuid.UUID) -> ToolContext:
    """A tool context for a machine principal in one tenant.

    `tenant_id` is the load-bearing part: the counts are scoped to the caller's
    own tenant the same way every other read tool's are.
    """
    return ToolContext(
        db=db,
        redis=redis,
        principal=Principal(
            kind="service_account",
            tenant_id=tenant_id,
            scopes=frozenset({Scope.TELEMETRY_READ.value}),
        ),
    )


async def _call(
    db: AsyncSession, redis: Any, tenant_id: uuid.UUID
) -> GetOutboxStatusOutput:
    return await get_outbox_status(GetOutboxStatusInput(), _ctx(db, redis, tenant_id))


async def _add_event(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    created_seconds_ago: float = 0.0,
    published_seconds_ago: float | None = None,
    failed: bool = False,
    attempts: int = 0,
) -> OutboxEvent:
    now = datetime.now(UTC)
    published_at = (
        None
        if published_seconds_ago is None
        else now - timedelta(seconds=published_seconds_ago)
    )
    row = OutboxEvent(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        topic="job.submitted",
        key=f"{tenant_id}:{uuid.uuid4()}",
        payload={"event": "job.submitted"},
        attempts=attempts,
        created_at=now - timedelta(seconds=created_seconds_ago),
        published_at=published_at,
        failed_at=published_at if failed else None,
    )
    db.add(row)
    await db.flush()
    return row


async def _second_tenant(db: AsyncSession) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    db.add(Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[:8]}", name="other"))
    await db.flush()
    return tenant_id


# ---------------------------------------------------------------------------
# A healthy outbox reads healthy
# ---------------------------------------------------------------------------


async def test_an_empty_outbox_reports_zero_and_nulls_not_a_crash(
    db_session: AsyncSession, default_tenant: Any
) -> None:
    """The empty case is the healthy case, and it has to be sayable.

    A zero count with null ages — not an exception, and not a null count that a
    caller would have to interpret.
    """
    out = await _call(db_session, _RedisStub(), default_tenant.id)

    assert out.unpublished_count == 0
    assert out.oldest_unpublished_at is None
    assert out.oldest_unpublished_age_s is None
    assert out.newest_unpublished_at is None
    assert out.newest_unpublished_age_s is None
    assert out.unpublished_past_attempt_limit == 0
    assert out.last_publish_at is None
    assert out.seconds_since_last_publish is None
    assert out.measured_at.tzinfo is not None


async def test_a_drained_outbox_reports_its_last_delivery_and_nothing_waiting(
    db_session: AsyncSession, default_tenant: Any
) -> None:
    """The ordinary healthy reading: rows exist, all delivered, none waiting."""
    await _add_event(
        db_session, default_tenant.id, created_seconds_ago=30, published_seconds_ago=29
    )
    await _add_event(
        db_session, default_tenant.id, created_seconds_ago=5, published_seconds_ago=4
    )

    out = await _call(db_session, _RedisStub(), default_tenant.id)

    assert out.unpublished_count == 0
    assert out.oldest_unpublished_age_s is None
    assert out.last_publish_at is not None
    assert out.seconds_since_last_publish is not None
    # The newest real delivery, not the oldest.
    assert 3 <= out.seconds_since_last_publish <= 10


async def test_an_idle_platform_is_not_reported_as_a_stall(
    db_session: AsyncSession, default_tenant: Any
) -> None:
    """An old `last_publish_at` with nothing waiting is silence, not a fault.

    The reading has to be able to express "nothing was submitted for an hour",
    because the description tells the caller to read `last_publish_at` together
    with `unpublished_count` and that instruction is only honest if the pair
    really does separate the two worlds.
    """
    await _add_event(
        db_session,
        default_tenant.id,
        created_seconds_ago=3700,
        published_seconds_ago=3699,
    )

    out = await _call(db_session, _RedisStub(), default_tenant.id)

    assert out.unpublished_count == 0
    assert out.seconds_since_last_publish is not None
    assert out.seconds_since_last_publish > 3600


# ---------------------------------------------------------------------------
# A stalled outbox reads stalled
# ---------------------------------------------------------------------------


async def test_a_stalled_outbox_brackets_the_backlog_in_one_reading(
    db_session: AsyncSession, default_tenant: Any
) -> None:
    """Oldest far above the tick interval, newest fresh: arriving, not leaving.

    This pair is what makes the trend readable from a single call, which is the
    claim the description makes, so it is asserted as a pair rather than as two
    independent numbers.
    """
    await _add_event(db_session, default_tenant.id, created_seconds_ago=300)
    await _add_event(db_session, default_tenant.id, created_seconds_ago=120)
    await _add_event(db_session, default_tenant.id, created_seconds_ago=1)

    out = await _call(db_session, _RedisStub(), default_tenant.id)

    assert out.unpublished_count == 3
    assert out.oldest_unpublished_age_s is not None
    assert out.newest_unpublished_age_s is not None
    assert out.oldest_unpublished_age_s >= 290
    assert out.newest_unpublished_age_s <= 30
    assert out.oldest_unpublished_age_s > out.newest_unpublished_age_s
    assert out.oldest_unpublished_age_s > RELAY_TICK_INTERVAL_SECONDS


async def test_an_abandoned_row_is_never_reported_as_a_delivery(
    db_session: AsyncSession, default_tenant: Any
) -> None:
    """`mark_failed` stamps `published_at` too — so the filter is load-bearing.

    Without `failed_at IS NULL` in the predicate, the reading below would say
    the relay published a second ago while the queue was stalled: the worst
    possible answer, arrived at silently. Written through the repository's own
    `mark_failed` rather than by hand so the test cannot drift from what the
    relay actually writes.
    """
    row = await _add_event(db_session, default_tenant.id, created_seconds_ago=60)
    await _add_event(db_session, default_tenant.id, created_seconds_ago=30)
    await OutboxRepository(db_session).mark_failed([row.id], "abandoned")

    out = await _call(db_session, _RedisStub(), default_tenant.id)

    assert out.last_publish_at is None, (
        "an abandoned row was reported as the last successful delivery"
    )
    assert out.seconds_since_last_publish is None
    # The abandoned row left the waiting set — it is no longer awaiting
    # delivery, it was given up on.
    assert out.unpublished_count == 1


async def test_rows_the_relay_will_not_retry_are_counted_and_separated(
    db_session: AsyncSession, default_tenant: Any
) -> None:
    """A backlog made of capped rows does not drain, however healthy the relay.

    They are inside `unpublished_count` (they really are awaiting delivery) and
    also counted on their own, so a caller is not left to infer that the queue
    will clear itself.
    """
    cap = 5
    with patch(
        "app.repositories.outbox.get_settings",
        return_value=_settings_with_cap(cap),
    ):
        await _add_event(
            db_session, default_tenant.id, created_seconds_ago=600, attempts=cap
        )
        await _add_event(
            db_session, default_tenant.id, created_seconds_ago=600, attempts=cap + 3
        )
        await _add_event(
            db_session, default_tenant.id, created_seconds_ago=10, attempts=0
        )

        out = await _call(db_session, _RedisStub(), default_tenant.id)

    assert out.unpublished_count == 3
    assert out.unpublished_past_attempt_limit == 2


async def test_another_tenants_backlog_is_not_counted(
    db_session: AsyncSession, default_tenant: Any
) -> None:
    """Tenant-scoped, like every other read tool (and like RLS underneath)."""
    other = await _second_tenant(db_session)
    await _add_event(db_session, other, created_seconds_ago=500)
    await _add_event(db_session, default_tenant.id, created_seconds_ago=10)

    mine = await _call(db_session, _RedisStub(), default_tenant.id)
    theirs = await _call(db_session, _RedisStub(), other)

    assert mine.unpublished_count == 1
    assert mine.oldest_unpublished_age_s is not None
    assert mine.oldest_unpublished_age_s < 100
    assert theirs.unpublished_count == 1
    assert theirs.oldest_unpublished_age_s is not None
    assert theirs.oldest_unpublished_age_s > 400


# ---------------------------------------------------------------------------
# Every age is measured against one clock
# ---------------------------------------------------------------------------


async def test_every_age_is_measured_at_the_time_the_reading_says(
    db_session: AsyncSession, default_tenant: Any
) -> None:
    """`measured_at` minus the timestamp equals the age, for every pair.

    The point is not arithmetic, it is that a caller can never be handed an age
    and a timestamp that contradict each other — which is what happens when the
    ages are taken from one clock and the timestamps from another.
    """
    await _add_event(db_session, default_tenant.id, created_seconds_ago=240)
    await _add_event(db_session, default_tenant.id, created_seconds_ago=20)
    await _add_event(
        db_session, default_tenant.id, created_seconds_ago=90, published_seconds_ago=88
    )
    redis = _RedisStub(
        {RELAY_TICK_KEY: (datetime.now(UTC) - timedelta(seconds=45)).isoformat()}
    )

    out = await _call(db_session, redis, default_tenant.id)

    for stamp, age in (
        (out.oldest_unpublished_at, out.oldest_unpublished_age_s),
        (out.newest_unpublished_at, out.newest_unpublished_age_s),
        (out.last_publish_at, out.seconds_since_last_publish),
        (out.relay_last_tick_at, out.relay_heartbeat_age_s),
    ):
        assert stamp is not None and age is not None
        assert age == pytest.approx(
            (out.measured_at - stamp).total_seconds(), abs=1.5
        )


async def test_a_clock_ahead_of_the_reading_reports_zero_not_a_negative_age(
    db_session: AsyncSession, default_tenant: Any
) -> None:
    """The worker and the database are two hosts; skew must not print a minus.

    Clamped rather than passed through: a negative age is not a thing a caller
    can act on, and the description says so where the field is documented.
    """
    redis = _RedisStub(
        {RELAY_TICK_KEY: (datetime.now(UTC) + timedelta(seconds=120)).isoformat()}
    )

    out = await _call(db_session, redis, default_tenant.id)

    assert out.relay_heartbeat_known is True
    assert out.relay_heartbeat_age_s == 0.0


# ---------------------------------------------------------------------------
# The relay heartbeat: written by the pass, read as unknown when absent
# ---------------------------------------------------------------------------


async def test_the_tool_reports_the_tick_the_relay_recorded() -> None:
    """Writer and reader agree on the key, the format and the meaning."""
    redis = _RedisStub()
    at = datetime.now(UTC) - timedelta(seconds=7)

    await record_relay_tick(redis, now=at)
    read_at, reason = await read_relay_tick(redis)

    assert redis.ttls[RELAY_TICK_KEY] == RELAY_TICK_TTL_SECONDS
    assert read_at == at
    assert reason is None


async def test_the_tick_key_is_not_in_the_lab_namespace() -> None:
    """A platform signal with a platform writer.

    Under `chaos:*` it would be swept by the reset script and it would imply
    something about *why* the relay is not running, which this reading must
    never do.
    """
    assert not RELAY_TICK_KEY.startswith("chaos:")
    assert RELAY_TICK_KEY == "outbox:relay:last_tick"


@pytest.mark.parametrize(
    ("stored", "expected_reason"),
    [
        (None, TICK_UNKNOWN_NO_RECORD),
        ("not-a-timestamp", TICK_UNKNOWN_UNREADABLE),
        ("", TICK_UNKNOWN_UNREADABLE),
    ],
)
async def test_an_unusable_tick_record_is_unknown_with_the_reason(
    db_session: AsyncSession,
    default_tenant: Any,
    stored: str | None,
    expected_reason: str,
) -> None:
    """Unknown is null plus the reason — never a fabricated zero.

    A `0` here would read as "the relay ticked at the instant you asked", which
    is the single most misleading thing this field could say.
    """
    redis = _RedisStub({} if stored is None else {RELAY_TICK_KEY: stored})

    out = await _call(db_session, redis, default_tenant.id)

    assert out.relay_heartbeat_known is False
    assert out.relay_last_tick_at is None
    assert out.relay_heartbeat_age_s is None
    assert out.relay_heartbeat_unknown_reason == expected_reason


async def test_an_unreachable_store_is_unknown_with_its_own_reason(
    db_session: AsyncSession, default_tenant: Any
) -> None:
    """A read failure is a third, distinguishable kind of not-knowing.

    It must not take the rest of the reading down with it: the counts come from
    the database and are still true.
    """
    redis = _RedisStub()
    redis.raise_on_get = True
    await _add_event(db_session, default_tenant.id, created_seconds_ago=42)

    out = await _call(db_session, redis, default_tenant.id)

    assert out.relay_heartbeat_known is False
    assert out.relay_heartbeat_unknown_reason == TICK_UNKNOWN_UNREACHABLE
    assert out.unpublished_count == 1


async def test_recording_a_tick_never_costs_the_relay_its_pass() -> None:
    """Fail open on the write, matching `loop_is_paused`.

    The relay exists to deliver events. A diagnostic write that could raise
    inside the tick would trade a real outage for a reading.
    """
    redis = _RedisStub()
    redis.raise_on_set = True

    await record_relay_tick(redis)  # must not raise

    assert RELAY_TICK_KEY not in redis.store


async def test_the_relay_records_its_pass_from_inside_the_tick(
    sqlite_engine: Any,
) -> None:
    """The stamp is written by the work, not by the loop around it.

    Written before the leader gate and the pause check it would say "the
    coroutine is alive", which is not the question. Written inside the pass it
    says "a pass ran", which is. Exercised through the real
    `_outbox_relay_tick` on an empty queue — the early-return path, because an
    idle relay is exactly the case a heartbeat exists for.
    """
    from app.workers import dispatcher
    from sqlalchemy.ext.asyncio import async_sessionmaker

    redis = _RedisStub()
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)

    with (
        patch("app.core.redis.get_redis_client", return_value=redis),
        patch("app.workers.dispatcher.metrics.emit_gauge", new=AsyncMock()),
    ):
        await dispatcher._outbox_relay_tick(factory)

    assert RELAY_TICK_KEY in redis.store
    recorded, reason = await read_relay_tick(redis)
    assert reason is None
    assert recorded is not None
    assert abs((datetime.now(UTC) - recorded).total_seconds()) < 30


def test_the_mirrored_tick_interval_matches_the_relay() -> None:
    """The tool advertises the relay's poll interval, so the two must agree.

    Mirrored rather than imported: the MCP process does not import the worker
    package (see `consumer_lag.py`'s sample-key mirror for the same shape and
    the same reason). A mirror without a tripwire is a lie waiting to happen.
    """
    from app.workers.dispatcher import OUTBOX_RELAY_INTERVAL

    assert RELAY_TICK_INTERVAL_SECONDS == OUTBOX_RELAY_INTERVAL


# ---------------------------------------------------------------------------
# The contract delta, pinned
# ---------------------------------------------------------------------------


def test_the_read_tier_gained_exactly_this_tool() -> None:
    """A tool-surface delta is a contract delta; this is the whole of it.

    Chaos tools are unregistered under default unit settings, so the count here
    is the non-chaos surface: 20 tools before this order, 21 after. With
    `CHAOS_ENABLED=true` the eleven chaos tools join and the number the
    commander pins moves 31 → 32.
    """
    read = sorted(
        t.name
        for t in list_tools()
        if t.required_scope in {Scope.TELEMETRY_READ, Scope.INCIDENTS_READ}
    )
    assert read == READ_TIER_AFTER
    assert len(list_tools()) == 21


def test_the_tool_declares_the_telemetry_read_scope() -> None:
    """Delivery health is observability, and it is the partner reading to
    `get_consumer_lag` — the same scope, so one token sees both halves of the
    contrast or neither."""
    td = next(t for t in list_tools() if t.name == "get_outbox_status")
    assert td.required_scope is Scope.TELEMETRY_READ
    assert td.is_chaos is False
    assert td.is_idempotent is False


def test_the_shape_of_the_new_tool_is_exactly_this() -> None:
    """The field list the rebless note needs, read off the registry."""
    td = next(t for t in list_tools() if t.name == "get_outbox_status")

    assert td.input_json_schema().get("properties", {}) == {}
    assert set(td.output_json_schema()["properties"]) == OUTPUT_FIELDS


def test_no_class_docstring_reaches_the_pinned_schema() -> None:
    """plat #210, generalised: a class docstring becomes a wire `description`.

    Pydantic copies a model's class docstring into the schema's top-level
    `description`, and an enum's into `$defs.<Enum>.description`. This tool has
    no enum field and must have no docstring on either model — the prose that
    explains them belongs in comments and in the tool description, which is
    written for the caller.
    """
    assert GetOutboxStatusInput.__doc__ is None
    assert GetOutboxStatusOutput.__doc__ is None

    td = next(t for t in list_tools() if t.name == "get_outbox_status")
    for schema in (td.input_json_schema(), td.output_json_schema()):
        assert "description" not in schema, (
            "a class docstring leaked into the pinned tool schema"
        )
        assert "$defs" not in schema, (
            "a nested model or enum appeared in this tool's schema — check its "
            "class docstring before the schema is pinned"
        )


@pytest.mark.parametrize(
    "phrase",
    [
        # Say which clock (rule 1).
        "database server's clock",
        "clock",
        # State pagination explicitly.
        "no `offset`",
        "nothing is truncated",
        # Unknown is not zero.
        "UNKNOWN IS NULL, NEVER 0",
        # A healthy reading looks healthy, and the trap in it is named.
        "WHAT A HEALTHY OUTBOX LOOKS LIKE",
        # What the reading does not prove.
        "does not say why",
        # Never advertise what the tool cannot deliver (rule 4).
        "WHAT THIS TOOL CANNOT SEE",
    ],
)
def test_the_description_carries_the_sentences_it_is_required_to(
    phrase: str,
) -> None:
    """The description *is* the interface (incident-platform/CLAUDE.md §naming).

    Each phrase here stands for one of the four normative description rules or
    for one of the two misreadings this reading is most likely to produce. They
    are pinned because a later edit that drops one is a functional regression
    with no other symptom.
    """
    td = next(t for t in list_tools() if t.name == "get_outbox_status")
    assert phrase in td.description


def test_the_description_never_says_why_the_relay_is_not_running() -> None:
    """ADR 0012: the reading is the same whatever stopped the relay.

    A stopped relay reads as "last pass N seconds ago" and nothing more. The
    wider vocabulary screen lives in `test_lab_invisibility.py`; this is the
    narrower claim for this tool, phrased as the words a well-meaning author
    would reach for.
    """
    td = next(t for t in list_tools() if t.name == "get_outbox_status")
    lowered = (td.description + td.output_json_schema().__str__()).lower()
    for word in ("chaos", "paused", "pause", "deliberately stopped", "injected"):
        assert word not in lowered, f"the description names {word!r}"


def _settings_with_cap(cap: int) -> Any:
    from app.config import Settings

    return Settings(environment="test", outbox_max_attempts=cap)
