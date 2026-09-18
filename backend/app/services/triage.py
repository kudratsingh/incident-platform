"""
LLM-driven triage for dead-lettered jobs.

Asks Claude to classify the root cause, summarise the failure and suggest a fix;
persisted to `job_triages` so an admin can choose Replay vs Resolve without reading a
stack trace. `messages.parse()` shape-checks the response against `TriageAnalysis` before
the DB sees it; `claude-opus-4-7` with adaptive thinking, taxonomy prompt cached.
"""

import asyncio
import json
from typing import Any, Literal

import anthropic
from app.config import get_settings
from app.core.logging import get_logger
from app.models.enums import RemediationHint
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
    """The Pydantic schema the model fills in — one fixed shape for every triage."""

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
# Analysis → remediation category
# ---------------------------------------------------------------------------

# Below this, the analysis is a guess and gets no category (R2-24) — NULL already means
# "not categorised, not replay-safe". Both directions are gated: a low-confidence
# `replay_safe` re-fails, and a low-confidence `human_required` escalates needlessly.
_MIN_HINT_CONFIDENCE = 0.5

# Retryable, but not *yet* — the distinction `wait_and_replay` carries. Replaying now
# burns the retry against a dependency that is still down.
_DEPENDENCY_CATEGORIES = frozenset({"external_api_failure", "infrastructure"})


def remediation_hint_for(analysis: TriageAnalysis) -> str | None:
    """Map a `TriageAnalysis` onto a `RemediationHint`, or None.

    None is the default and a real answer — NULL is what every DLQ tool treats as
    "unknown, not replay-safe". `is_retryable` is read first as the narrower question.
    """
    if analysis.confidence < _MIN_HINT_CONFIDENCE:
        return None
    if analysis.root_cause_category == "unknown":
        # "Too generic to classify confidently", by the taxonomy's own
        # definition. Any category derived from it would be invented.
        return None
    if not analysis.is_retryable:
        return RemediationHint.HUMAN_REQUIRED.value
    if analysis.root_cause_category in _DEPENDENCY_CATEGORIES:
        return RemediationHint.WAIT_AND_REPLAY.value
    return RemediationHint.REPLAY_SAFE.value


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

# Frozen across requests so it caches cleanly; per-request data goes in the user message.
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
    max_attempts: int,
    trace_id: str | None,
) -> tuple[TriageAnalysis, dict[str, Any], str]:
    """Call Claude and return (analysis, usage_dict, model_id).

    Raises TriageDisabledError when off; anthropic errors and `llm_triage_timeout_seconds`
    timeouts propagate (ADR 0005). The caller owns the fallback, not this service.
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
        "max_attempts": max_attempts,
        "trace_id": trace_id,
        "error_message": error_message,
        "payload": payload or {},
    }

    # System prompt as a list so we can attach cache_control to the last
    # block — caches the taxonomy + platform description across all triages.
    async def _call() -> Any:
        return await client.messages.parse(
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

    response = await asyncio.wait_for(
        _call(), timeout=settings.llm_triage_timeout_seconds
    )

    analysis = response.parsed_output
    if analysis is None:
        # Refusal or schema mismatch — surface as a structured error.
        raise RuntimeError(
            f"triage parse returned no output (stop_reason={response.stop_reason})"
        )
    return analysis, extract_usage(response), settings.llm_triage_model
