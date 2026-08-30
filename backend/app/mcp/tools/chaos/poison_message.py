"""
`poison_message` — publish a schema-invalid payload to a Kafka topic
AND drop a matching `replay_safe` DLQ entry.

Two effects (both observable via `list_dlq_messages` +
`get_consumer_lag`):

1. **Kafka side.** Sends a schema-invalid payload; the target
   consumer's `_process_one` catches `SchemaValidationError`, logs,
   commits, and moves on. Bypasses `publish_raw` (which validates)
   using an inline short-lived aiokafka producer.

2. **DLQ side.** Writes a synthetic `jobs` row with
   `status=dead_letter`, `remediation_hint=replay_safe`, and a
   realistic error string. That's how the agent's remediation loop
   sees the poisoning — the platform's real consumers don't route
   schema errors to DLQ (they log-and-drop), so without this
   synthetic row the agent would see no downstream effect.

Requires `chaos:invoke`. Registered only when `CHAOS_ENABLED=true`.
"""

import json

from app.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext
from app.models.enums import JobStatus, JobType, RemediationHint
from app.models.job import Job
from app.models.user import User
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select


class PoisonMessageBrokerUnavailableError(AppError):
    """Kafka broker isn't reachable from the MCP process. The tool
    can't drop the poisoned message so the DLQ side-effect also
    doesn't fire. Returned instead of a generic -32603 so the caller
    sees a specific, actionable error."""

    status_code = 503
    error_code = "kafka_unavailable"


class PoisonMessageSendFailedError(AppError):
    """The broker answered but refused the send — unknown topic, no
    partition leader, message too large, ACL denial.

    Deliberately NOT `kafka_unavailable`: that code names an
    unreachable broker, and the operator response differs (bring the
    broker up vs. create the topic / fix the payload). Before R2-16
    only `start()` was inside the broad catch, so every one of these
    escaped as `-32603 internal tool error` — which the commander's
    ChaosClient buckets as a transport fault, hiding a fixture bug as
    flakiness."""

    status_code = 502
    error_code = "kafka_send_failed"


logger = get_logger(__name__)


class PoisonMessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(
        min_length=1,
        max_length=128,
        description="Kafka topic name. Examples: 'job.submitted', "
        "'job.progress'. The message will fail schema validation on the "
        "consumer side because we pass a payload the schema rejects.",
    )
    # Kept as a dict so the operator can craft exactly which shape gets
    # sent. Default is an empty object — every topic's schema requires
    # some fields, so `{}` reliably poisons every one.
    payload: dict[str, object] = Field(
        default_factory=dict,
        description="Payload to send. Defaults to `{}`, which fails every "
        "topic's schema because they all require specific fields.",
    )
    partition_key: str | None = Field(
        default=None,
        description="Optional Kafka message key. Omit to let the broker "
        "hash by partition round-robin.",
    )


class PoisonMessageOutput(BaseModel):
    topic: str
    payload_bytes: int
    partition_key: str | None = None
    accepted: bool = Field(
        description="Kafka send succeeded from the platform's "
        "perspective. The paired synthetic DLQ entry is guaranteed."
    )
    dlq_job_id: str = Field(
        description="ID of the synthetic DLQ entry the hook wrote "
        "(remediation_hint=replay_safe). Observable via "
        "`list_dlq_messages`. Always populated on a successful call: "
        "an unseeded tenant gets a lazy-created chaos owner rather "
        "than a skipped row, so `accepted` and this field can no "
        "longer disagree."
    )


@chaos_tool(
    "poison_message",
    description=(
        "Publish a schema-invalid payload to one Kafka topic AND "
        "drop a synthetic `replay_safe` DLQ entry the agent's "
        "remediation loop can act on. Bypasses producer-side "
        "validation; real consumers log-and-drop schema errors "
        "rather than routing to DLQ, so the synthetic row makes "
        "the effect observable through `list_dlq_messages`."
    ),
    input_model=PoisonMessageInput,
    output_model=PoisonMessageOutput,
    blast_radius=BlastRadius.SINGLE_CONSUMER,
)
async def poison_message(
    inp: PoisonMessageInput, ctx: ToolContext
) -> PoisonMessageOutput:
    # Import inline so the aiokafka dep isn't required for chaos-disabled
    # environments where this tool never registers.
    from aiokafka import AIOKafkaProducer  # type: ignore[import-untyped]

    settings = get_settings()
    body = json.dumps(inp.payload).encode()
    key_bytes = inp.partition_key.encode() if inp.partition_key else None

    # aiokafka's error class is behind the inline import, so catch
    # broadly and surface a clean AppError. `start()` is inside the
    # try so a bootstrap failure still hits `stop()` — otherwise the
    # producer object leaks with an "Unclosed AIOKafkaProducer"
    # warning.
    producer = AIOKafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
    )
    try:
        try:
            await producer.start()
        except Exception as exc:
            raise PoisonMessageBrokerUnavailableError(
                f"Kafka broker not reachable at "
                f"{settings.kafka_bootstrap_servers}: {exc}"
            ) from exc
        try:
            await producer.send_and_wait(inp.topic, value=body, key=key_bytes)
        except Exception as exc:
            raise PoisonMessageSendFailedError(
                f"Kafka refused the send to topic {inp.topic!r}: {exc}"
            ) from exc
    finally:
        try:
            await producer.stop()
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "aiokafka producer stop failed",
                extra={"error": str(exc)},
            )

    # Synthetic DLQ entry — the observable effect the agent's
    # remediation loop keys off. Real consumers log+commit schema
    # errors rather than routing to DLQ, so without this row the
    # agent has nothing to hypothesize about.
    #
    # An unseeded tenant is the normal case on a fresh eval stack, not a
    # defensive edge (R2-16). Skipping the row there while still
    # answering `accepted=true` made the scenario silently unwinnable and
    # mis-scored the agent — a wasted paid run. Both sibling hooks
    # (`create_bad_data_job`, `seed_dlq_messages`) lazy-create the same
    # chaos owner for exactly this case, and reusing their helper means
    # the reset's `_delete_chaos_owner_users` sweep reaches this row too.
    tenant_id = ctx.principal.tenant_id
    user = (
        await ctx.db.execute(
            select(User).where(User.tenant_id == tenant_id).limit(1)
        )
    ).scalar_one_or_none()
    if user is None:
        from app.mcp.tools.chaos.create_bad_data_job import _ensure_chaos_owner

        user = await _ensure_chaos_owner(ctx, tenant_id)

    error_msg = (
        f"SchemaValidationError: payload missing required field on "
        f"topic '{inp.topic}' (chaos poison_message)"
    )
    job = Job(
        tenant_id=tenant_id,
        user_id=user.id,
        type=JobType.BULK_API_SYNC.value,
        status=JobStatus.DEAD_LETTER.value,
        payload={"chaos_fixture": "poison_message", "topic": inp.topic},
        retry_count=3,
        error_message=error_msg,
        remediation_hint=RemediationHint.REPLAY_SAFE.value,
    )
    ctx.db.add(job)
    await ctx.db.flush()
    dlq_job_id = str(job.id)

    logger.warning(
        "chaos poison_message sent",
        extra={
            "topic": inp.topic,
            "bytes": len(body),
            "key": inp.partition_key,
            "dlq_job_id": dlq_job_id,
        },
    )
    return PoisonMessageOutput(
        topic=inp.topic,
        payload_bytes=len(body),
        partition_key=inp.partition_key,
        accepted=True,
        dlq_job_id=dlq_job_id,
    )
