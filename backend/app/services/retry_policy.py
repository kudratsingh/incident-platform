"""
LLM-guided retry policy.

When a job fails *and* it still has retries left, ask Claude whether to:
  * keep retrying (with what backoff), or
  * dead-letter immediately because the failure is clearly not transient.

This sits on top of the deterministic exponential-backoff retry — the LLM
only refines the decision once the first deterministic retry has already
happened (see `llm_retry_policy_min_retry_count`). Any error from the LLM
call (disabled flag, missing API key, timeout, network blip, schema mismatch)
falls back to deterministic behavior. The worker never blocks on the API.

The pattern mirrors `app/services/triage.py`:
  * `messages.parse()` with a Pydantic schema — no raw JSON parsing.
  * Default `claude-opus-4-7` with adaptive thinking.
  * Frozen system prompt with `cache_control: ephemeral` so the policy
    description caches across calls.
"""

import asyncio
import json
from typing import Any, Literal

import anthropic
from app.config import get_settings
from app.core.logging import get_logger
from pydantic import BaseModel, Field

logger = get_logger(__name__)


RetryAction = Literal["retry_with_backoff", "dead_letter_now"]


class RetryDecision(BaseModel):
    """Shape Claude fills in. `dead_letter_now` short-circuits remaining
    deterministic retries; `retry_with_backoff` lets the worker enqueue with
    the recommended backoff."""

    action: RetryAction = Field(
        description=(
            "retry_with_backoff: enqueue this job for another attempt after "
            "`backoff_seconds`. dead_letter_now: the failure is clearly not "
            "transient — skip remaining retries and move to the DLQ."
        )
    )
    backoff_seconds: int = Field(
        description=(
            "How long to wait before the next attempt. Ignored when action is "
            "dead_letter_now. Capped at 3600s upstream."
        ),
        ge=0,
        le=3600,
    )
    reasoning: str = Field(
        description="One short sentence explaining why.",
        max_length=400,
    )


class RetryPolicyDisabledError(RuntimeError):
    """Raised when the LLM-guided policy isn't configured. Caller falls back."""


_SYSTEM_PROMPT = """\
You are a retry-policy advisor for a background job platform.

When a job fails, you decide whether the next attempt should run (and after
how long) or whether to give up immediately and dead-letter the job. The
platform already retries deterministically with exponential backoff —
you are consulted only after the first retry, so you have one prior failure
to learn from.

Bias toward `retry_with_backoff` for:
  - network blips, 5xx responses from external APIs, brief rate-limit hits
  - any error wording that mentions "timeout", "temporarily", "connection",
    "rate limit", "throttled", "retry"
  - infrastructure errors (DB connection refused, Redis OOM) — these
    typically self-heal in seconds to minutes

Bias toward `dead_letter_now` for:
  - authentication / authorization failures (401, 403, "invalid credentials")
  - validation errors ("schema invalid", "missing field", "type mismatch")
  - explicit "not found" responses for resources the job needs
  - any error that has now failed twice with the same message — the issue
    is clearly not transient

Backoff guidance:
  - For transient infra: 30-120 seconds
  - For external API rate limits: 60-300 seconds
  - For unclear / first time seeing this error: 60 seconds and let the
    deterministic policy take it the rest of the way

Reasoning should be one short sentence — admins read it in the audit log
when investigating retry patterns.
"""


def is_enabled() -> bool:
    return bool(get_settings().llm_retry_policy_enabled)


async def decide_retry(
    job_type: str,
    error_message: str,
    retry_count: int,
    max_retries: int,
    prior_error: str | None = None,
) -> tuple[RetryDecision, dict[str, Any], str]:
    """Call Claude and return (decision, usage_dict, model_id).

    Raises RetryPolicyDisabledError when the feature is off, anthropic /
    network exceptions on real API failures, asyncio.TimeoutError when the
    call exceeds `llm_retry_policy_timeout_seconds`.
    """
    settings = get_settings()
    if not settings.llm_retry_policy_enabled:
        raise RetryPolicyDisabledError(
            "LLM retry policy disabled (set LLM_RETRY_POLICY_ENABLED=1)"
        )

    client = anthropic.AsyncAnthropic()

    user_payload = {
        "job_type": job_type,
        "current_error": error_message,
        "prior_error": prior_error,
        "retry_count": retry_count,
        "max_retries": max_retries,
    }

    async def _call() -> Any:
        return await client.messages.parse(
            model=settings.llm_retry_policy_model,
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
                        "A job just failed. Decide whether to retry it and "
                        "respond with a RetryDecision.\n\n"
                        f"```json\n{json.dumps(user_payload, indent=2, default=str)}\n```"
                    ),
                }
            ],
            output_format=RetryDecision,
        )

    response = await asyncio.wait_for(
        _call(), timeout=settings.llm_retry_policy_timeout_seconds
    )

    decision = response.parsed_output
    if decision is None:
        raise RuntimeError(
            f"retry policy parse returned no output (stop_reason={response.stop_reason})"
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
    return decision, usage, settings.llm_retry_policy_model
