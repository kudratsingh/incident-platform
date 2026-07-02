"""
LLM-driven triage for dead-lettered jobs.

When a job exhausts its retries and dead-letters, this service asks Claude
to classify the root cause, summarise the failure, and suggest a fix. The
result is persisted to `job_triages` and surfaced to admins so they don't
have to read raw stack traces to decide between Replay and Resolve.

Implementation notes
--------------------
- Uses the Anthropic Python SDK with `messages.parse()` to get a validated
  Pydantic object back — the model's response is shape-checked against
  `TriageAnalysis` before we touch the DB.
- Default model is `claude-opus-4-7` with adaptive thinking — failure
  diagnosis is an intelligence-sensitive task and the cost is per-DLQ, not
  per-request, so paying for the top tier is the right call.
- System prompt is cached: it carries the failure taxonomy + the platform
  description and never changes across triages, so the per-call cost is
  dominated by the (small) per-job context.
"""

import json
from typing import Any, Literal

import anthropic
from app.config import get_settings
from app.core.logging import get_logger
from app.services._llm_usage import extract_usage
from pydantic import BaseModel, Field

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


RootCauseCategory = Literal[
    "external_api_failure",
    "validation_error",
    "infrastructure",
    "data_corruption",
    "configuration",
    "transient",
    "unknown",
]


class TriageAnalysis(BaseModel):
    """The Pydantic schema the model fills in. Constrains every triage to the
    same fixed shape so the admin UI can render it without conditional logic."""

    root_cause_category: RootCauseCategory = Field(
        description="One of the fixed categories that best fits the failure."
    )
    summary: str = Field(
        description=(
            "One sentence (under ~30 words) describing what actually went wrong."
        ),
        max_length=400,
    )
    suggested_fix: str = Field(
        description=(
            "Concrete next action for the on-call admin. Mention the specific "
            "field, dependency, or config to change. One or two sentences."
        ),
        max_length=600,
    )
    is_retryable: bool = Field(
        description=(
            "True iff replaying the job as-is is reasonably likely to succeed "
            "(e.g. transient network blip). False if the failure is "
            "deterministic (validation error, missing config)."
        )
    )
    confidence: float = Field(
        description="Your own confidence in this analysis, 0.0 to 1.0.",
        ge=0.0,
        le=1.0,
    )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

# Frozen across requests so it caches cleanly. No timestamps or per-request
# data is interpolated here — those go in the user message after the
# cache_control breakpoint.
_SYSTEM_PROMPT = """\
You are the on-call triage assistant for an internal Incident & Workflow
Platform. Each job in the system goes through retries; when it exhausts
them it lands in a dead-letter queue. Your task is to classify the
failure and suggest a concrete next step for an admin.

Root cause categories (pick the one that fits best):

  external_api_failure  An upstream service returned an error, timed out,
                        or refused the request. Includes auth failures
                        against a third-party API.
  validation_error      Input payload was malformed or violated a domain
                        invariant. Replaying as-is will fail again.
  infrastructure        Our own infra is the problem — DB pool exhausted,
                        Redis unreachable, OOM in the worker.
  data_corruption       The data being processed is bad in a way that
                        validation didn't catch (truncated file, broken
                        encoding, dangling FK).
  configuration         Missing or wrong configuration (feature flag,
                        secret, environment variable). Common at deploy
                        boundaries.
  transient             A blip with no clear root cause attributable to
                        the categories above — likely retryable.
  unknown               The error is too generic to classify confidently.

Rules:
- Be concrete. "Check the logs" is not a suggested fix. Name the field,
  the dependency, or the config knob that's likely at fault.
- If the error message mentions a specific identifier (URL, status code,
  exception class, file path), quote it back in your summary.
- Set is_retryable=true only when a replay has a real chance of success.
  Most validation errors and configuration errors are NOT retryable.
- Confidence should reflect how diagnostic the error message actually is.
  A bare "RuntimeError: boom" gets low confidence; a 504 with a clear
  upstream hostname gets high.
"""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TriageDisabledError(RuntimeError):
    """Raised when triage is requested but the feature isn't configured."""


def is_enabled() -> bool:
    return bool(get_settings().llm_triage_enabled)


async def triage_failure(
    job_type: str,
    payload: dict[str, Any] | None,
    error_message: str,
    retry_count: int,
    max_retries: int,
    trace_id: str | None,
) -> tuple[TriageAnalysis, dict[str, Any], str]:
    """Call Claude and return (analysis, usage_dict, model_id).

    Raises TriageDisabledError when triage is off, or anthropic exceptions
    on real API failures so the caller can decide whether to retry or skip.
    """
    settings = get_settings()
    if not settings.llm_triage_enabled:
        raise TriageDisabledError("LLM triage is disabled (set LLM_TRIAGE_ENABLED=1)")

    # The SDK reads ANTHROPIC_API_KEY from the env. Failing fast here with a
    # clear message beats waiting for a 401 inside the retry path.
    client = anthropic.AsyncAnthropic()

    user_payload = {
        "job_type": job_type,
        "retry_count": retry_count,
        "max_retries": max_retries,
        "trace_id": trace_id,
        "error_message": error_message,
        "payload": payload or {},
    }

    # System prompt as a list so we can attach cache_control to the last
    # block — caches the taxonomy + platform description across all triages.
    response = await client.messages.parse(
        model=settings.llm_triage_model,
        max_tokens=2000,
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
                    "A job has dead-lettered. Analyse it and respond with a "
                    "TriageAnalysis.\n\n"
                    f"```json\n{json.dumps(user_payload, indent=2, default=str)}\n```"
                ),
            }
        ],
        output_format=TriageAnalysis,
    )

    analysis = response.parsed_output
    if analysis is None:
        # Refusal or schema mismatch — surface as a structured error.
        raise RuntimeError(
            f"triage parse returned no output (stop_reason={response.stop_reason})"
        )
    return analysis, extract_usage(response), settings.llm_triage_model
