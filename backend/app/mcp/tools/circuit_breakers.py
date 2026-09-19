"""
`get_circuit_breakers` — whether the platform has stopped calling something.

A breaker that has opened is the difference between "a downstream dependency is failing" and
"something inside this platform is wrong", and nothing exposed it: the registry is in-process
and the reader is in another one (ADR 0006), so the state is read from the record each breaker
writes for exactly this purpose (ADR 0030). Needs `telemetry:read`.
"""

from datetime import UTC, datetime

from app.core.breaker_state import BreakerRecord, read_breaker_states
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from pydantic import BaseModel, ConfigDict, Field


class GetCircuitBreakersInput(BaseModel):
    # No fields and `extra="forbid"`: the description promises no filtering or paging.
    model_config = ConfigDict(extra="forbid")


class CircuitBreakerReading(BaseModel):
    name: str = Field(
        description="Which dependency this breaker guards. A fixed platform name, the "
        "same across calls and restarts."
    )
    state: str = Field(
        description="`closed` (calls go through), `open` (calls are refused without "
        "being attempted) or `half_open` (one trial call is allowed through; every "
        "other caller is refused while it is in flight)."
    )
    failure_count: int = Field(
        description="Consecutive failures counted toward `failure_threshold`. Reset to 0 "
        "by any success. A 0 here is a measurement, not a missing value."
    )
    failure_threshold: int = Field(
        description="How many consecutive failures open this breaker. The yardstick "
        "`failure_count` is read against: 2 of 3 is a dependency in trouble, 2 of 20 is "
        "noise."
    )
    recovery_timeout_s: float = Field(
        description="How long an open breaker waits before letting one trial call "
        "through, in seconds."
    )
    last_state_change_at: datetime | None = Field(
        default=None,
        description="When this breaker last changed state — ISO-8601, UTC, on the clock "
        "of the process that owns the breaker, not the one that answered this call. "
        "`null` when it has never changed state, which for a breaker that has always "
        "been closed is the healthy case.",
    )
    seconds_since_state_change: float | None = Field(
        default=None,
        description="How long ago `last_state_change_at` was, in seconds, against this "
        "reading's own clock. It compares two processes' clocks, so treat a second "
        "either way as noise. `null` exactly when `last_state_change_at` is null.",
    )
    last_failure_at: datetime | None = Field(
        default=None,
        description="When a call through this breaker last failed, on the owning "
        "process's clock. `null` when none has failed since the platform started.",
    )
    last_failure_reason_class: str | None = Field(
        default=None,
        description="What kind of failure the last one was: `timeout` (the call ran out "
        "of time), `connection` (the far side could not be reached) or `other` — which "
        "does NOT distinguish an error the far side reported from a bug on this side, "
        "because the breaker cannot tell. Never the error text. `null` when nothing has "
        "failed.",
    )
    recorded_at: datetime = Field(
        description="When the owning process last wrote this record, on its own clock."
    )
    reported_age_s: float = Field(
        description="How long ago this record was written, in seconds. **This is not a "
        "heartbeat.** A record is written whenever the state changes and refreshed while "
        "calls keep flowing, so a large age on a closed breaker means no calls have gone "
        "through it lately — not that anything has stopped."
    )


class GetCircuitBreakersOutput(BaseModel):
    measured_at: datetime = Field(
        description="When this reading was taken — ISO-8601, UTC, the clock of the "
        "process that answered the call. Every `*_age_s` and `seconds_since_*` value is "
        "this time minus the timestamp beside it."
    )
    breakers: tuple[CircuitBreakerReading, ...] = Field(
        description="Every breaker with a state record, in name order. Not a page: no "
        "cap, nothing truncated."
    )
    total: int = Field(
        description="How many breakers are in `breakers`. Always equal to its length."
    )
    unknown_reason: str | None = Field(
        default=None,
        description="Why no breaker state is known, in plain words. When this is set, "
        "`breakers` is empty because the platform could not tell you anything — which is "
        "not the same as no breaker being open. `null` whenever at least one breaker "
        "reported.",
    )


def _reading(record: BreakerRecord, measured_at: datetime) -> CircuitBreakerReading:
    """One record, with its ages taken against the reader's clock."""
    return CircuitBreakerReading(
        name=record.name,
        state=record.state,
        failure_count=record.failure_count,
        failure_threshold=record.failure_threshold,
        recovery_timeout_s=record.recovery_timeout_s,
        last_state_change_at=record.last_state_change_at,
        seconds_since_state_change=_age_seconds(
            measured_at, record.last_state_change_at
        ),
        last_failure_at=record.last_failure_at,
        last_failure_reason_class=record.last_failure_reason_class,
        recorded_at=record.recorded_at,
        reported_age_s=_age_seconds(measured_at, record.recorded_at) or 0.0,
    )


def _age_seconds(measured_at: datetime, at: datetime | None) -> float | None:
    """Seconds between a timestamp and the reading, clamped at 0 for clock skew."""
    if at is None:
        return None
    return round(max(0.0, (measured_at - at).total_seconds()), 3)


@tool(
    "get_circuit_breakers",
    description=(
        "Read whether this platform has stopped calling any of its "
        "dependencies. A circuit breaker sits in front of each one: after "
        "enough consecutive failures it opens and refuses calls without "
        "attempting them, then after a wait lets one trial call through. This "
        "reading reports each breaker's state, how many consecutive failures "
        "are counted against it, when it last changed state and what kind of "
        "failure it last saw.\n"
        "WHAT IT DISCRIMINATES. An open breaker says the platform's calls to "
        "something outside it were failing. That is a different fault from slow "
        "queries or a saturated connection pool (`get_postgres_health`) and from "
        "work piling up inside the platform (`get_consumer_lag`, "
        "`get_outbox_status`), and it points outward rather than inward.\n"
        "FRESHNESS AND WHICH CLOCK. `measured_at` is the clock of the process "
        "that answered this call, and every age is that clock minus the "
        "timestamp beside it. The timestamps themselves are written by the "
        "process that owns the breaker, on its clock, so an age compares two "
        "clocks — treat a second either way as noise.\n"
        "NO PAGING, NOTHING CAPPED. It takes no arguments. There is no `limit`, "
        "no `offset` and no filter; every breaker with a record comes back, and "
        "`total` is a count of them rather than a page size.\n"
        "AN AGE HERE IS NOT A HEARTBEAT. A record is written whenever a breaker "
        "changes state, and refreshed while calls keep flowing through it. So a "
        "large `reported_age_s` on a `closed` breaker means nothing has called "
        "that dependency lately — it is not evidence that anything stopped, and "
        "not evidence that the owning process is alive.\n"
        "ABSENT IS UNKNOWN, NOT CLOSED. A breaker with no record is not listed "
        "at all, and an empty listing with `unknown_reason` set means the "
        "platform could tell you nothing — neither of those is a breaker "
        "reporting itself closed. Read `unknown_reason` before concluding "
        "anything from an empty list.\n"
        "SCOPE. Breakers are platform-wide, shared by every tenant: this "
        "reading is not scoped to yours, and an open breaker affects everyone.\n"
        "WHAT THIS CANNOT SEE. It reports state, counts and times, and nothing "
        "else: no error text, no endpoint, no request that failed, and no "
        "history — each call is the state now, so it cannot show how long a "
        "breaker has been flapping."
    ),
    input_model=GetCircuitBreakersInput,
    output_model=GetCircuitBreakersOutput,
    required_scope=Scope.TELEMETRY_READ,
)
async def get_circuit_breakers(
    _inp: GetCircuitBreakersInput, ctx: ToolContext
) -> GetCircuitBreakersOutput:
    records, unknown_reason = await read_breaker_states(ctx.redis)
    measured_at = datetime.now(UTC)
    readings = tuple(_reading(record, measured_at) for record in records)
    return GetCircuitBreakersOutput(
        measured_at=measured_at,
        breakers=readings,
        total=len(readings),
        unknown_reason=unknown_reason,
    )


__all__ = [
    "CircuitBreakerReading",
    "GetCircuitBreakersInput",
    "GetCircuitBreakersOutput",
    "get_circuit_breakers",
]
