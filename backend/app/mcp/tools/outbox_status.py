"""
`get_outbox_status` — how the transactional outbox is delivering right now.

Every job state change commits with a row in `outbox_events` that a background relay
publishes to Kafka (ADR 0001, 0020, 0028). A stopped consumer climbs
`get_consumer_lag` and leaves the outbox empty; a stopped relay does the opposite,
and this tool is the only read that shows it (WO-R3-201). Nothing says *why* — a
deliberate stop must read like a dead process (ADR 0012). Needs `telemetry:read`.
"""

from datetime import datetime

from app.core.outbox_heartbeat import read_relay_tick
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.repositories.outbox import OutboxRepository
from pydantic import BaseModel, ConfigDict, Field

# Mirrored from `dispatcher.OUTBOX_RELAY_INTERVAL`, not imported: the MCP process
# must not import the worker package (`tests/unit/test_outbox_status.py` fails if
# one side moves). On the wire because an age means nothing without it: 3 s is
# normal, 300 is a stopped relay.
RELAY_TICK_INTERVAL_SECONDS = 1.0


class GetOutboxStatusInput(BaseModel):
    # No fields and `extra="forbid"`: the description promises no filtering or paging.
    model_config = ConfigDict(extra="forbid")


class GetOutboxStatusOutput(BaseModel):
    measured_at: datetime = Field(
        description="When this reading was taken — ISO-8601, UTC, the database "
        "server's clock. Every age below is this time minus the timestamp "
        "beside it."
    )
    unpublished_count: int = Field(
        description="Events committed to the database and not yet delivered to "
        "Kafka, for your tenant. An exact count of all of them, not a page. 0 "
        "means the queue is empty right now, which is the healthy steady state."
    )
    oldest_unpublished_at: datetime | None = Field(
        default=None,
        description="When the longest-waiting undelivered event was committed. "
        "`null` exactly when `unpublished_count` is 0 — there is no oldest row "
        "because there are no rows.",
    )
    oldest_unpublished_age_s: float | None = Field(
        default=None,
        description="How long the longest-waiting undelivered event has been "
        "waiting, in seconds. This is the number that says whether delivery is "
        "behind: compare it with `relay_tick_interval_s`. `null` exactly when "
        "`unpublished_count` is 0.",
    )
    newest_unpublished_at: datetime | None = Field(
        default=None,
        description="When the most recently committed undelivered event was "
        "committed. `null` exactly when `unpublished_count` is 0.",
    )
    newest_unpublished_age_s: float | None = Field(
        default=None,
        description="Age of the most recently committed undelivered event, in "
        "seconds. Read it with `oldest_unpublished_age_s`: a small value here "
        "beside a large one there means events are still arriving while none "
        "are leaving. `null` exactly when `unpublished_count` is 0.",
    )
    unpublished_past_attempt_limit: int = Field(
        description="How many of `unpublished_count` have already been tried "
        "the maximum number of times and will not be tried again. They are "
        "inside the total, not beside it. A backlog made entirely of these "
        "will not drain however healthy the relay is; one where this is 0 "
        "drains as soon as delivery resumes."
    )
    last_publish_at: datetime | None = Field(
        default=None,
        description="When an event of yours was last successfully delivered to "
        "Kafka. Counts successful deliveries only — an event the relay gave up "
        "on is never reported here. `null` when no event of yours has ever "
        "been delivered.",
    )
    seconds_since_last_publish: float | None = Field(
        default=None,
        description="How long ago `last_publish_at` was, in seconds. `null` "
        "exactly when `last_publish_at` is null. On its own this proves "
        "nothing: it is equally large on a stalled platform and on a quiet "
        "one. Read it with `unpublished_count`.",
    )
    relay_last_tick_at: datetime | None = Field(
        default=None,
        description="When the relay last completed a pass over the queue, as "
        "the relay itself recorded it on the worker's clock. It records a pass "
        "whether or not there was anything to deliver, so a fresh value here "
        "on an empty queue means the relay is running and idle. `null` when "
        "the platform has no record to report — see "
        "`relay_heartbeat_unknown_reason`.",
    )
    relay_heartbeat_age_s: float | None = Field(
        default=None,
        description="How long ago the relay last completed a pass, in seconds. "
        "`null` exactly when `relay_last_tick_at` is null, which means unknown "
        "— never 0, because a 0 would read as a relay that had just run. "
        "Clamped at 0 at the other end: a recorded time later than "
        "`measured_at` means the worker's and the database's clocks disagree, "
        "and is reported as 0 rather than as a negative age.",
    )
    relay_heartbeat_known: bool = Field(
        description="True when `relay_last_tick_at` is a real record, "
        "including one that is very old — an old pass is a finding, not a "
        "missing reading. False when the platform has nothing to report, which "
        "is missing information and evidence of neither a running nor a "
        "stopped relay."
    )
    relay_heartbeat_unknown_reason: str | None = Field(
        default=None,
        description="Why the relay pass time is unknown, in plain words. "
        "`null` exactly when `relay_heartbeat_known` is true.",
    )
    relay_tick_interval_s: float = Field(
        description="How often the relay is configured to look at the queue, "
        "in seconds. This is the yardstick for the two numbers above: an "
        "`oldest_unpublished_age_s` or `relay_heartbeat_age_s` within a few "
        "multiples of it is normal, and one hundreds of times larger is not."
    )


def _age_seconds(measured_at: datetime, at: datetime | None) -> float | None:
    """Seconds between a timestamp and the moment the reading was taken.

    Clamped at 0: a negative age is worker/database clock skew, and the field
    description says so.
    """
    if at is None:
        return None
    return round(max(0.0, (measured_at - at).total_seconds()), 3)


@tool(
    "get_outbox_status",
    description=(
        "Read how the platform's transactional outbox is delivering: how many "
        "events are committed and still waiting to reach Kafka, how long they "
        "have been waiting, when one was last delivered, and when the relay "
        "that delivers them last ran.\n"
        "WHAT THIS MEASURES. Every job state change is written to the database "
        "together with an outbox row in one transaction; a background relay "
        "polls those rows and publishes them to Kafka. A growing number of "
        "waiting rows therefore means writes are landing and delivery is not "
        "keeping up with them — the queue between the database and the broker, "
        "which is not the same queue as consumer lag.\n"
        "FRESHNESS AND WHICH CLOCK. Nothing here is cached. The counts and "
        "timestamps come from one query run at call time, and `measured_at` is "
        "the database server's clock at that moment; every `*_age_s` and "
        "`seconds_since_*` value is that clock minus the timestamp beside it, "
        "so an age and its timestamp can never disagree. One exception worth "
        "knowing: `relay_last_tick_at` is recorded by the worker process on "
        "ITS clock, so `relay_heartbeat_age_s` compares two hosts and carries "
        "whatever skew is between them — treat a few seconds either way there "
        "as noise.\n"
        "NO PAGING, NOTHING CAPPED. This tool returns counts and timestamps, "
        "never rows. There is no `limit`, no `offset`, nothing is truncated, "
        "and `unpublished_count` is an exact count of every waiting event "
        "rather than a page of them. It takes no arguments.\n"
        "SCOPE. The counts and timestamps cover your own tenant's events only. "
        "The relay is a single process serving every tenant, so "
        "`relay_last_tick_at` and `relay_heartbeat_age_s` are platform-wide: "
        "they say whether the relay is running at all, not whether it is "
        "getting to your tenant.\n"
        "ONE CALL SHOWS WHETHER THE QUEUE IS DRAINING. "
        "`oldest_unpublished_age_s` and `newest_unpublished_age_s` bracket the "
        "backlog in a single reading. A relay that is keeping up leaves an "
        "oldest age within a few multiples of `relay_tick_interval_s`. An "
        "oldest age far above that while the newest is fresh means events are "
        "arriving and not leaving — visible without waiting and without a "
        "second call. Because the query is live rather than read from a cache, "
        "a second call a few seconds later is genuine new evidence.\n"
        "WHAT A HEALTHY OUTBOX LOOKS LIKE. `unpublished_count` at or near 0; "
        "`oldest_unpublished_age_s` null or within a few multiples of "
        "`relay_tick_interval_s`; `relay_heartbeat_age_s` a small number of "
        "seconds. One healthy reading looks alarming and is not: on a platform "
        "with little traffic `last_publish_at` can be hours old while "
        "`unpublished_count` is 0, which means nothing was submitted, not that "
        "delivery stopped. Read `last_publish_at` together with "
        "`unpublished_count`, never on its own.\n"
        "WHAT AN OLD RELAY PASS DOES AND DOES NOT PROVE. A "
        "`relay_heartbeat_age_s` far above `relay_tick_interval_s` means the "
        "relay has not completed a pass in that long. It does not say why, and "
        "it is not by itself a delivery problem: an old pass with "
        "`unpublished_count: 0` means nothing is waiting, so nothing is being "
        "harmed. The pair is the finding — a backlog that is aging AND a relay "
        "that has not run.\n"
        "UNKNOWN IS NULL, NEVER 0. `relay_heartbeat_known: false` with both "
        "`relay_last_tick_at` and `relay_heartbeat_age_s` null means the "
        "platform has no pass to report; `relay_heartbeat_unknown_reason` says "
        "which case it is. That is missing information — evidence of neither a "
        "running relay nor a stopped one. The unpublished timestamps and their "
        "ages are null exactly when `unpublished_count` is 0, which is the "
        "healthy case rather than a gap: there is no oldest row because there "
        "are no rows. `last_publish_at` is null when no event of yours has "
        "ever been delivered.\n"
        "DELIVERED MEANS DELIVERED. `last_publish_at` reports successful "
        "deliveries only. A row the relay abandoned is marked internally in a "
        "way that would otherwise look like a delivery, and it is excluded "
        "here, so this timestamp never reports a giving-up as a publish. "
        "`unpublished_past_attempt_limit` is the other half of that story: "
        "those rows are inside `unpublished_count` and will not be retried.\n"
        "WHAT THIS TOOL CANNOT SEE. It reads the database's handoff queue and "
        "the relay's own record of its last pass, and nothing else. It says "
        "nothing about whether Kafka retained what was published, nothing "
        "about consumers reading from Kafka (`get_consumer_lag` is that "
        "reading), and a delivery recorded here is the relay's own account of "
        "a successful publish, not an acknowledgement from anything "
        "downstream."
    ),
    input_model=GetOutboxStatusInput,
    output_model=GetOutboxStatusOutput,
    required_scope=Scope.TELEMETRY_READ,
)
async def get_outbox_status(
    _inp: GetOutboxStatusInput, ctx: ToolContext
) -> GetOutboxStatusOutput:
    snapshot = await OutboxRepository(ctx.db).delivery_snapshot(
        tenant_id=ctx.principal.tenant_id
    )
    # Never fatal: a missing heartbeat reads unknown, the counts still stand.
    tick_at, unknown_reason = await read_relay_tick(ctx.redis)

    return GetOutboxStatusOutput(
        measured_at=snapshot.measured_at,
        unpublished_count=snapshot.unpublished_count,
        oldest_unpublished_at=snapshot.oldest_unpublished_at,
        oldest_unpublished_age_s=_age_seconds(
            snapshot.measured_at, snapshot.oldest_unpublished_at
        ),
        newest_unpublished_at=snapshot.newest_unpublished_at,
        newest_unpublished_age_s=_age_seconds(
            snapshot.measured_at, snapshot.newest_unpublished_at
        ),
        unpublished_past_attempt_limit=snapshot.unpublished_past_attempt_limit,
        last_publish_at=snapshot.last_publish_at,
        seconds_since_last_publish=_age_seconds(
            snapshot.measured_at, snapshot.last_publish_at
        ),
        relay_last_tick_at=tick_at,
        relay_heartbeat_age_s=_age_seconds(snapshot.measured_at, tick_at),
        relay_heartbeat_known=tick_at is not None,
        relay_heartbeat_unknown_reason=unknown_reason,
        relay_tick_interval_s=RELAY_TICK_INTERVAL_SECONDS,
    )


__all__ = [
    "RELAY_TICK_INTERVAL_SECONDS",
    "GetOutboxStatusInput",
    "GetOutboxStatusOutput",
    "get_outbox_status",
]
