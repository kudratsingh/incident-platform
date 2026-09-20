import uuid
from datetime import datetime
from typing import Any, Literal

from app.schemas.common import PaginationParams
from pydantic import BaseModel, ConfigDict, Field


class AuditLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID | None
    # Canonical actor identity going forward. `user_id` remains populated
    # for human actors so existing clients don't break.
    principal_type: str = "user"
    principal_id: uuid.UUID | None = None
    job_id: uuid.UUID | None
    action: str
    resource_type: str | None
    resource_id: str | None
    request_id: str | None
    ip_address: str | None
    extra_data: dict[str, Any] | None
    created_at: datetime


#: How many prefixes one request may name, per parameter. Each becomes a LIKE, so the
#: bound is on the query's cost; ten is more streams than this platform has.
MAX_ACTION_PREFIXES = 10


def _prefixes(raw: str | None) -> tuple[str, ...]:
    """A comma-separated prefix list, cleaned: blanks dropped, duplicates dropped,
    order kept, bounded at `MAX_ACTION_PREFIXES`.

    A list that is all blanks (`","`) reads as no filter rather than as a filter nothing
    matches — the caller asked for nothing, not for an empty page.
    """
    if raw is None:
        return ()
    seen: dict[str, None] = {}
    for part in raw.split(","):
        cleaned = part.strip()
        if cleaned:
            seen.setdefault(cleaned, None)
    return tuple(seen)[:MAX_ACTION_PREFIXES]


class AuditListParams(PaginationParams):
    user_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    action: str | None = None
    # `action` is an exact match, which cannot isolate a stream: `agent.`,
    # `chaos.` and `job.` are prefixes, and the console's audit views filter on
    # whole streams. Bounded because it reaches a LIKE pattern.
    #
    # A COMMA LIST since WO-R3-328 (`agent.,lab.,chaos.`): the operator streams are
    # three prefixes and the console wanted them in one request rather than three it
    # would have to merge and re-sort client-side. One prefix is still one prefix.
    action_prefix: str | None = Field(default=None, max_length=200)
    # Whole streams to leave out — also a comma list. `event.` is the case it exists
    # for: the traffic loop writes 40-odd `event.job.*` rows a minute, which buried the
    # three rows a demo is about. Exclusion wins over `action_prefix` where the two
    # overlap, because a row this names is a row the caller said it did not want.
    exclude_prefix: str | None = Field(default=None, max_length=200)
    # Isolate operator activity (`user`) from agent activity
    # (`service_account`). Omit to see both.
    principal_type: Literal["user", "service_account"] | None = None

    def action_prefixes(self) -> tuple[str, ...]:
        return _prefixes(self.action_prefix)

    def exclude_prefixes(self) -> tuple[str, ...]:
        return _prefixes(self.exclude_prefix)
