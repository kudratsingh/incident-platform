"""
`poison_message` — publish a schema-invalid payload to a Kafka topic.

Real effect: the target consumer's `_process_one` catches
`SchemaValidationError`, logs, commits, and moves on. The metric the
agent should watch is the corresponding structured-log warning and any
per-tenant DLQ movement that results.

Bypasses the normal `publish_raw` path — that path validates the
payload and would reject the poison payload before it reached the
broker. Uses a short-lived aiokafka producer inline so we don't
touch the shared platform producer's send buffer.

Requires `chaos:invoke`. Registered only when `CHAOS_ENABLED=true`.
"""

import json

from app.config import get_settings
from app.core.logging import get_logger
from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext
from pydantic import BaseModel, ConfigDict, Field

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
        description="Whether the send succeeded from the platform's "
        "perspective. Doesn't guarantee the consumer has processed it — "
        "the resulting DLQ movement (if any) is observable via "
        "`list_dlq_messages`."
    )


@chaos_tool(
    "poison_message",
    description=(
        "Publish a schema-invalid payload to one Kafka topic, bypassing "
        "the platform's producer-side schema validation. The target "
        "consumer will parse-fail, log, commit, and move on — this "
        "exercises the error path without corrupting real messages."
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

    producer = AIOKafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
    )
    await producer.start()
    try:
        await producer.send_and_wait(inp.topic, value=body, key=key_bytes)
    finally:
        await producer.stop()

    logger.warning(
        "chaos poison_message sent",
        extra={
            "topic": inp.topic,
            "bytes": len(body),
            "key": inp.partition_key,
        },
    )
    return PoisonMessageOutput(
        topic=inp.topic,
        payload_bytes=len(body),
        partition_key=inp.partition_key,
        accepted=True,
    )
