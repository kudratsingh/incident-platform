"""
Natural-language admin queries.

Lets an admin type a sentence ("CSV uploads that failed in the last hour
with retry_count >= 2") and get back a constrained filter spec that maps
directly to existing list endpoints — no raw SQL ever leaves the model.

Safety
------
The model can ONLY fill in fields on `JobFilterSpec`. Every field is an
enum, a small typed value, or a bounded integer. Pydantic rejects
anything else. That means even a prompt-injected user input can't smuggle
unsafe data into the query — the worst case is a benign filter spec that
returns zero rows.

Pattern matches `triage.py` and `retry_policy.py`:
  * messages.parse() with a Pydantic schema
  * claude-opus-4-7 with adaptive thinking
  * frozen system prompt with cache_control: ephemeral
"""

import json
from datetime import UTC, datetime

import anthropic
from app.config import get_settings
from app.core.logging import get_logger
from app.models.enums import JobStatus, JobType
from pydantic import BaseModel, Field

logger = get_logger(__name__)


class JobFilterSpec(BaseModel):
    """The constrained shape Claude returns. Every field is optional —
    if the user's question doesn't constrain something, the model leaves
    it null and the API treats it as "no filter."""

    status: JobStatus | None = Field(
        default=None,
        description="One of: waiting, pending, running, completed, failed, dead_letter, cancelled.",
    )
    type: JobType | None = Field(
        default=None,
        description="One of: csv_upload, report_gen, bulk_api_sync, doc_analysis.",
    )
    trace_id: str | None = Field(
        default=None,
        description="A specific trace id to look up — only set if the user pasted one.",
        max_length=64,
    )
    created_after: datetime | None = Field(
        default=None,
        description=(
            "ISO 8601 UTC timestamp. Use for 'after X', 'since X', 'in the last N hours/days'. "
            "For relative phrases, compute the absolute timestamp from `now`."
        ),
    )
    created_before: datetime | None = Field(
        default=None,
        description=(
            "ISO 8601 UTC timestamp. Use for 'before X', 'older than N hours/days'. "
            "For relative phrases, compute the absolute timestamp from `now`."
        ),
    )
    retry_count_min: int | None = Field(
        default=None,
        description="Lower bound on retry_count (inclusive). Use for 'at least N retries'.",
        ge=0,
        le=100,
    )
    retry_count_max: int | None = Field(
        default=None,
        description="Upper bound on retry_count (inclusive). Use for 'at most N retries'.",
        ge=0,
        le=100,
    )
    limit: int = Field(
        default=20,
        description=(
            "How many rows to return. Defaults to 20. Honor the user when they "
            "say 'top 5' / 'first 50' etc., clamped to [1, 100]."
        ),
        ge=1,
        le=100,
    )


class NLQueryDisabledError(RuntimeError):
    """Raised when NL queries are requested but the feature isn't configured."""


_SYSTEM_PROMPT = """\
You translate plain-English admin questions about background jobs into a
JobFilterSpec — a small, fixed-shape filter object the platform applies to
its `GET /admin/jobs` endpoint.

What this is NOT: do not write SQL, do not invent fields, do not include
free-text search. The platform applies your filter via SQLAlchemy and
returns the matching rows.

Status values (use the exact strings):
  waiting       - blocked on parent jobs in a DAG
  pending       - queued, about to run
  running       - currently executing
  completed     - finished successfully
  failed        - failed but will retry
  dead_letter   - exhausted retries; in the DLQ
  cancelled     - cancelled by a saga rollback or by an admin

Job types (use the exact strings):
  csv_upload, report_gen, bulk_api_sync, doc_analysis

Time handling: phrases like "in the last hour", "since 9am", "yesterday"
must be converted to ISO 8601 UTC timestamps in `created_after` /
`created_before`. The current time is given to you as `now` in the user
message. Never put a timestamp in the future for `created_after`.

Common patterns:
  "failed CSV uploads"                   → status=failed, type=csv_upload
  "dead-lettered jobs in the last hour"  → status=dead_letter, created_after=now-1h
  "report generations with at least 2 retries"
                                         → type=report_gen, retry_count_min=2
  "top 5 stuck jobs"                     → status=running, limit=5

When the user is unclear, leave the relevant field null. Returning a
spec with zero filters is fine — the UI shows the most recent rows in
that case.
"""


def is_enabled() -> bool:
    return bool(get_settings().llm_nl_query_enabled)


async def parse_question(question: str) -> tuple[JobFilterSpec, dict[str, int], str]:
    """Call Claude and return (filter_spec, usage, model_id).

    Raises NLQueryDisabledError when the feature is off, anthropic /
    network exceptions on real API failures.
    """
    settings = get_settings()
    if not settings.llm_nl_query_enabled:
        raise NLQueryDisabledError(
            "NL query disabled (set LLM_NL_QUERY_ENABLED=1)"
        )

    client = anthropic.AsyncAnthropic()

    now = datetime.now(UTC).isoformat()
    user_payload = {"now": now, "question": question}

    response = await client.messages.parse(
        model=settings.llm_nl_query_model,
        max_tokens=1024,
        thinking={"type": "adaptive"},
        system=[
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    "Convert this question into a JobFilterSpec.\n\n"
                    f"```json\n{json.dumps(user_payload, indent=2)}\n```"
                ),
            }
        ],
        output_format=JobFilterSpec,
    )

    spec = response.parsed_output
    if spec is None:
        raise RuntimeError(
            f"nl query parse returned no output (stop_reason={response.stop_reason})"
        )
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_creation_input_tokens": getattr(
            response.usage, "cache_creation_input_tokens", 0
        ),
        "cache_read_input_tokens": getattr(
            response.usage, "cache_read_input_tokens", 0
        ),
    }
    return spec, usage, settings.llm_nl_query_model
