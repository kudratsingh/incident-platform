"""
Operator-facing shapes for `agent_runs` and the three readings the console needs
beside them (WO-R3-312).

These are REST models, read by a human operator's browser. They are deliberately NOT
the MCP tool shapes: the responder writes over MCP and reads nothing back (ADR 0035),
so nothing here is pinned in anyone's contract snapshot and nothing here reaches the
responder. Where a reading is unknown it says why, in its own field, rather than
defaulting to a zero an operator would read as healthy.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from app.schemas.common import PaginationParams
from pydantic import BaseModel, ConfigDict, Field, computed_field


class AgentRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    # The alert this run answers, or null when the responder named none.
    alert_id: uuid.UUID | None = None
    # The principal that wrote every report in this run.
    service_account_id: uuid.UUID
    # The responder's own short name for the run. Null when it sent none.
    scenario: str | None = None
    state: str
    # Append-only, oldest first: `[{"state": ..., "at": ...}]`. One entry per
    # transition, so consecutive entries never repeat a state.
    phase_history: list[dict[str, Any]] = Field(default_factory=list)
    current_hypothesis: dict[str, Any] | None = None
    last_step: dict[str, Any] | None = None
    # The responder's ranked explanations, BEST FIRST — the order is the ranking, and
    # `confidence` is its own number, never re-sorted here. Empty means it has reported
    # none, not that it has none.
    hypotheses: list[dict[str, Any]] = Field(default_factory=list)
    # What it decided to do and why, or null before it decided.
    plan: dict[str, Any] | None = None
    # The newest verify verdict, and every verdict this run produced, oldest first.
    # Bounded at 50 (`VERIFICATIONS_CAP`): past that the oldest are dropped.
    verification: dict[str, Any] | None = None
    verifications: list[dict[str, Any]] = Field(default_factory=list)
    # The action ledger, in the order the responder reported it. Bounded at 200
    # (`STEPS_CAP`) — read `steps_dropped` before describing it as the whole run, and
    # `GET /admin/agent-runs/{id}/steps?after_seq=` to poll it without re-reading this.
    steps: list[dict[str, Any]] = Field(default_factory=list)
    steps_dropped: int = 0
    # What the run has spent, on the responder's own meters. Null while it reported none.
    budget: dict[str, Any] | None = None
    # Present only once the run reported one; written once and never revised.
    briefing: dict[str, Any] | None = None
    started_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None

    @computed_field(  # type: ignore[prop-decorator]
        description=(
            "True while the run has no `finished_at` — nobody has reported a "
            "terminal state for it. Derived, not stored: there is one fact here "
            "and it is the timestamp."
        )
    )
    @property
    def active(self) -> bool:
        return self.finished_at is None


class AgentRunListParams(PaginationParams):
    # Runs answering one alert. Omit for every run in the tenant.
    alert_id: uuid.UUID | None = None
    # `true` = unfinished only, `false` = finished only, omitted = both.
    active: bool | None = None


class AgentRunStepResponse(BaseModel):
    """One entry in the action ledger, as the responder reported it.

    Every field but `seq` and `kind` can be null: this is the responder's own account of
    a call it made, and the platform fills nothing in. The excerpts are excerpts — the
    responder truncates them to 400 characters before sending, and the write surface
    refuses anything longer.
    """

    seq: int
    kind: str
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    result_excerpt: str | None = None
    outcome: str | None = None
    latency_ms: float | None = None
    at: datetime | None = None


class AgentRunStepsResponse(BaseModel):
    """A page of the ledger for one run, for a console polling it.

    Not `PaginatedResponse`: this is a tail read, not an offset page. The console asks
    for everything after the last `seq` it drew, so a slow poll costs one request rather
    than a re-read of the whole run, and a step it has already shown cannot arrive twice.
    """

    run_id: uuid.UUID
    # The run's state and closing time, so a poller knows when to stop asking.
    state: str
    finished_at: datetime | None = None
    # Ascending by `seq` — the order the responder did them in, which is the order a
    # ledger is read in. The console reverses it to show newest first.
    steps: list[AgentRunStepResponse] = Field(default_factory=list)
    # What this reply holds, and what the run holds now.
    returned: int
    total: int
    # How many of the oldest steps the cap discarded over this run's life. Anything but
    # 0 means `total` is the newest part of the run, not all of it.
    steps_dropped: int = 0
    # The `after_seq` this reply answered, echoed, and the one to send next time — the
    # highest `seq` stored, so a poll that returns nothing still advances correctly.
    after_seq: int | None = None
    next_after_seq: int | None = None


class ConsumerLagSampleResponse(BaseModel):
    lag: int
    measured_at: datetime


class ConsumerLagGroupResponse(BaseModel):
    consumer_group: str
    # Null is unknown, never zero — read `lag_unknown_reason` before comparing.
    lag: int | None = None
    lag_known: bool
    source: Literal["live", "static", "unrecognized"]
    # Why `lag` is null, in plain words. Null exactly when `lag_known` is true, so
    # an operator is never shown a blank cell with no explanation.
    lag_unknown_reason: str | None = None
    # When the number was measured, and how old it is. Both null for a group whose
    # value is a recorded constant: it was never measured at a moment.
    measured_at: datetime | None = None
    age_seconds: int | None = None
    # Last few measurements, newest first, the current one included. Empty for a
    # group nothing measures, and empty before the first window is recorded — an
    # empty list is missing history, not a flat line.
    recent_samples: list[ConsumerLagSampleResponse] = Field(default_factory=list)


class ConsumerLagResponse(BaseModel):
    measured_at: datetime
    groups: list[ConsumerLagGroupResponse]
    total: int
    # The one group whose number moves, named so a console can mark the others.
    live_group: str
    # How much history `recent_samples` can hold, and how far apart the measurements
    # are — so a chart can label its own axis from the reply instead of hard-coding the
    # platform's cadence (WO-R3-328). A window with fewer samples than it could hold is
    # missing history, never a flat line.
    sample_window_seconds: int
    sample_interval_seconds: int


class CircuitBreakerResponse(BaseModel):
    name: str
    state: str
    failure_count: int
    failure_threshold: int
    recovery_timeout_s: float
    last_state_change_at: datetime | None = None
    seconds_since_state_change: float | None = None
    last_failure_at: datetime | None = None
    last_failure_reason_class: str | None = None
    recorded_at: datetime
    # Not a heartbeat: a record is written on a state change and refreshed while
    # calls flow, so a large age on a closed breaker means nothing called that
    # dependency lately.
    reported_age_s: float


class CircuitBreakersResponse(BaseModel):
    measured_at: datetime
    breakers: list[CircuitBreakerResponse]
    total: int
    # Set when the platform could tell you nothing. An empty list with this set is
    # not the same finding as an empty list without it.
    unknown_reason: str | None = None


class AlertResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    severity: str
    source: str
    title: str
    description: str | None = None
    fired_at: datetime
    # Null while the alert is active.
    resolved_at: datetime | None = None
    extra_data: dict[str, Any] | None = None


class AlertListParams(PaginationParams):
    # `true` = unresolved only, `false` = resolved only, omitted = both.
    active: bool | None = None
    severity: str | None = None


__all__ = [
    "AgentRunListParams",
    "AgentRunResponse",
    "AgentRunStepResponse",
    "AgentRunStepsResponse",
    "AlertListParams",
    "AlertResponse",
    "CircuitBreakerResponse",
    "CircuitBreakersResponse",
    "ConsumerLagGroupResponse",
    "ConsumerLagResponse",
    "ConsumerLagSampleResponse",
]
