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
    "AlertListParams",
    "AlertResponse",
    "CircuitBreakerResponse",
    "CircuitBreakersResponse",
    "ConsumerLagGroupResponse",
    "ConsumerLagResponse",
    "ConsumerLagSampleResponse",
]
