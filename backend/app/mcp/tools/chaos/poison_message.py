"""`poison_message` — publish a schema-invalid payload to a Kafka topic AND drop
a matching dead-letter row that is **not** replay-safe.

Kafka side: an inline short-lived producer bypasses `publish_raw`'s validation;
the consumer logs, commits and moves on. DLQ side: a synthetic `jobs` row with a
schema-validation text from `app.lab.dlq_failure_stories`, because real consumers
log-and-drop schema errors rather than routing them, so without the row there is
nothing downstream to observe. WO-R2-166 moved the hint, not the text: a poisoned
message is never safe to replay, and both earlier pairings lied about it (live run
`efdc3b2a9864` graded a correct refusal as a failure). `unclassified` is the
default because LLM triage is off; `replay_safe` cannot be asked for, that row
being `create_mislabeled_dlq_job`'s job.

Ids are `uuid5(ns, f"{tenant_id}:{fixture_name}")`, pinnable and per-tenant. A
repeat is idempotent on the row and refused *before* the send once it has drifted,
so a refused call publishes nothing; publishing is never idempotent. Rows carry
`payload.seeded_fixture = true`, so the reset DELETEs them (ADR 0012 rule 2).
Requires `chaos:invoke`.
"""

import json
import uuid
from typing import Literal

from app.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.lab.dlq_failure_stories import story
from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext

# One spelling of "write NULL into remediation_hint", imported rather than
# restated. `_ensure_chaos_owner` is shared for a harder reason: the reset's
# `_delete_chaos_owner_users` only recognises that user by email prefix.
from app.mcp.tools.chaos.create_bad_data_job import (
    UNCLASSIFIED,
    _declared_hint,
    _ensure_chaos_owner,
)
from app.mcp.tools.chaos.seed_dlq_messages import SEEDED_FIXTURE_MARKER
from app.models.enums import JobStatus, JobType, RemediationHint
from app.models.job import Job
from app.models.user import User
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select


class PoisonMessageBrokerUnavailableError(AppError):
    """Broker unreachable: nothing published, no DLQ row. A specific
    code rather than a generic -32603."""

    status_code = 503
    error_code = "kafka_unavailable"


class PoisonMessageSendFailedError(AppError):
    """The broker answered but refused the send — unknown topic, no leader,
    message too large, ACL denial.

    NOT `kafka_unavailable`: different operator response, and before R2-16
    these escaped as `-32603`, which ChaosClient buckets as a transport fault."""

    status_code = 502
    error_code = "kafka_send_failed"


class PoisonMessageFixtureNameInUseError(AppError):
    """`fixture_name` names a row that no longer matches the call. Same 409 as
    `bad_data_fixture_name_in_use`: a drifted row is evidence."""

    status_code = 409
    error_code = "poison_fixture_name_in_use"


logger = get_logger(__name__)

# uuid5 namespace for poisoned-message fixture ids: uuid5(ns,
# f"{tenant_id}:{fixture_name}"). Distinct from every sibling hook's, so one
# `fixture_name` under two hooks is two rows. "dead" is the mnemonic.
_NAMESPACE = uuid.UUID("eeeeeeee-dead-4000-8000-000000000000")

# Both stories are schema-violation texts: the only difference is
# whether anybody classified it.
_STORY_KEY_FOR_HINT: dict[str | None, str] = {
    RemediationHint.HUMAN_REQUIRED.value: "schema_missing_field",
    None: "unclassified_schema_missing_field",
}


def fixture_id(tenant_id: uuid.UUID, fixture_name: str) -> uuid.UUID:
    """The row's deterministic id, exported so a test or a scenario derives it
    instead of transcribing the recipe. Per-tenant, or the RLS-scoped probe
    below would miss a sibling's row and collide: a 500 where 409 is promised.
    """
    return uuid.uuid5(_NAMESPACE, f"{tenant_id}:{fixture_name}")


def _dlq_error_for_topic(topic: str, hint: str | None) -> str:
    """The error text on the synthetic DLQ row.

    Module level so `tests/unit/test_dlq_text_coherence.py` can check this
    hook's (hint, text) pairs without a broker. The text is the
    schema-validation story for the declared hint — a permanent fault, which is
    what this hook injects — and names the topic and the producer correction
    without exposing the lab hook.
    """
    base = story(_STORY_KEY_FOR_HINT[hint]).error_message
    return (
        f"{base} (topic '{topic}': rejected by schema validation; "
        "producer must correct the payload)"
    )


class PoisonMessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(
        min_length=1,
        max_length=128,
        description="Kafka topic name. Examples: 'job.submitted', "
        "'job.progress'. The message will fail schema validation on the "
        "consumer side because we pass a payload the schema rejects.",
    )
    # A dict so the caller picks the shape; `{}` poisons every topic,
    # which all require fields.
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
    fixture_name: str = Field(
        default="poison-message",
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description=(
            "Name the dead-letter row's id derives from — "
            "uuid5(eeeeeeee-dead-4000-8000-000000000000, "
            "'{tenant_id}:{fixture_name}') — so a caller can pin the id "
            "before invoking. Its own namespace, so the same name under "
            "`create_bad_data_job` or `create_mislabeled_dlq_job` is a "
            "different row, not a collision. Ids are scoped to the "
            "calling tenant, so the same name in two tenants creates two "
            "independent rows. A repeat call is idempotent on the row "
            "while it still matches what the call declares, and refused "
            "with `poison_fixture_name_in_use` once it has drifted "
            "(someone fenced it, a replay moved it out of `dead_letter`, "
            "or this call declares a different `remediation_hint`); the "
            "refusal happens before the Kafka send, so nothing is "
            "published. The Kafka send itself is NOT idempotent — every "
            "accepted call publishes another poisoned message."
        ),
    )
    # A subset of `RemediationHint`: a schema violation is never safe to
    # replay; `create_mislabeled_dlq_job` owns the contradicting row.
    remediation_hint: Literal["human_required", "unclassified"] | None = Field(
        default=UNCLASSIFIED,
        description=(
            "Whether the dead-letter row arrives already classified. "
            "`unclassified` (the DEFAULT) — or JSON `null`, which means "
            "the same thing — writes `remediation_hint = NULL`: nothing "
            "has categorised the failure, which is how a freshly poisoned "
            "message really arrives, because LLM triage is off by default "
            "on this platform. `human_required` stamps the category, so "
            "`replay_dlq_by_category` refuses the row on sight and there "
            "is nothing left for a reader to decide. `replay_safe` is not "
            "accepted on either spelling: this hook injects a payload "
            "that fails schema validation, so every replay of it fails "
            "identically."
        ),
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
        description="ID of the synthetic DLQ entry this call guarantees "
        "exists. Observable via `list_dlq_messages`; NOT replay-safe. "
        "Derived from the calling tenant and `fixture_name`, so a caller "
        "can compute it in advance. Always populated on a successful "
        "call: an unseeded tenant gets a lazy-created chaos owner rather "
        "than a skipped row, so `accepted` and this field can no longer "
        "disagree."
    )
    fixture_name: str
    remediation_hint: str | None = Field(
        description="The category actually stamped on the dead-letter "
        "row: `human_required`, or null when the call declared "
        "`unclassified`. Never `replay_safe`."
    )
    created: bool = Field(
        description="False when the DLQ row was already present and "
        "still matching, so this call only published to Kafka."
    )


@chaos_tool(
    "poison_message",
    description=(
        "Publish a schema-invalid payload to one Kafka topic AND "
        "guarantee a synthetic dead-letter row the agent's remediation "
        "loop can act on. Bypasses producer-side validation; real "
        "consumers log-and-drop schema errors rather than routing to "
        "DLQ, so the synthetic row is what makes the effect observable "
        "through `list_dlq_messages`. THE ROW IS NOT REPLAY-SAFE: its "
        "error text is a schema violation (a required field missing from "
        "the stored payload), so every replay of it fails on the same "
        "field. `remediation_hint` decides only whether anything has "
        "classified that yet — `unclassified` (the default, and how a "
        "freshly poisoned message really arrives, because LLM triage is "
        "off by default here) leaves `remediation_hint` NULL, and "
        "`human_required` stamps the category so `replay_dlq_by_category` "
        "refuses the row on sight. `replay_safe` cannot be asked for. "
        "The row's id derives deterministically from the calling tenant "
        "and `fixture_name`, in its own namespace, so a caller can pin it "
        "before invoking and the same name under another fixture hook is "
        "a separate row; a repeat call is idempotent on the row while it "
        "still matches, and refused before publishing once it has "
        "drifted. Publishing is not idempotent — every accepted call puts "
        "another poisoned message on the topic. The row is tagged as a "
        "seeded fixture and DELETEd by the next environment reset."
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
    tenant_id = ctx.principal.tenant_id
    hint = _declared_hint(inp.remediation_hint)
    job_id = fixture_id(tenant_id, inp.fixture_name)

    # RLS-scoped, and BEFORE the producer: a refused call must not have
    # poisoned a topic.
    existing = (
        await ctx.db.execute(select(Job).where(Job.id == job_id))
    ).scalar_one_or_none()
    if existing is not None:
        _assert_matches(inp.fixture_name, existing, hint, tenant_id)

    body = json.dumps(inp.payload).encode()
    key_bytes = inp.partition_key.encode() if inp.partition_key else None

    # aiokafka's error class is behind the inline import, so catch broadly.
    # `start()` is inside the try so a bootstrap failure still hits `stop()`.
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

    if existing is not None:
        # Idempotent repeat: the row already matches, but the topic still
        # got another poisoned message.
        return PoisonMessageOutput(
            topic=inp.topic,
            payload_bytes=len(body),
            partition_key=inp.partition_key,
            accepted=True,
            dlq_job_id=str(job_id),
            fixture_name=inp.fixture_name,
            remediation_hint=hint,
            created=False,
        )

    # Synthetic DLQ entry — the observable effect the remediation loop keys
    # off. An unseeded tenant is the normal case on a fresh eval stack, not a
    # defensive edge (R2-16): skipping the row while answering `accepted=true`
    # made a scenario silently unwinnable. Reusing the siblings' helper keeps
    # the reset's `_delete_chaos_owner_users` able to reach this row.
    user = (
        await ctx.db.execute(
            select(User).where(User.tenant_id == tenant_id).limit(1)
        )
    ).scalar_one_or_none()
    if user is None:
        user = await _ensure_chaos_owner(ctx, tenant_id)

    error_msg = _dlq_error_for_topic(inp.topic, hint)
    job = Job(
        id=job_id,
        tenant_id=tenant_id,
        user_id=user.id,
        type=JobType.BULK_API_SYNC.value,
        status=JobStatus.DEAD_LETTER.value,
        # `seeded_fixture` is the disposal marker the reset sweep DELETEs
        # on; `chaos_fixture` stays for provenance (module docstring).
        payload={
            SEEDED_FIXTURE_MARKER: True,
            "chaos_fixture": "poison_message",
            "fixture_name": inp.fixture_name,
            "topic": inp.topic,
        },
        retry_count=3,
        error_message=error_msg,
        remediation_hint=hint,
    )
    ctx.db.add(job)
    await ctx.db.flush()

    logger.warning(
        "chaos poison_message sent",
        extra={
            "topic": inp.topic,
            "bytes": len(body),
            "key": inp.partition_key,
            "dlq_job_id": str(job_id),
            "fixture_name": inp.fixture_name,
            "remediation_hint": hint,
        },
    )
    return PoisonMessageOutput(
        topic=inp.topic,
        payload_bytes=len(body),
        partition_key=inp.partition_key,
        accepted=True,
        dlq_job_id=str(job_id),
        fixture_name=inp.fixture_name,
        remediation_hint=hint,
        created=True,
    )


def _assert_matches(
    fixture_name: str,
    existing: Job,
    hint: str | None,
    tenant_id: uuid.UUID,
) -> None:
    """Idempotent repeat vs. drifted row.

    Same rule as `create_bad_data_job._assert_matches`: still `dead_letter`, in
    the caller's tenant, with exactly the declared hint is a no-op on the row.
    Anything else is refused — a moved hint means somebody fenced it, which is
    what a scenario seeds an `unclassified` row to measure, and reporting
    `created=False` would hand the next run a pre-fenced world.
    """
    drift: list[str] = []
    if existing.tenant_id != tenant_id:
        # Unreachable while ids are tenant-derived; only a non-RLS
        # session could see a foreign row here.
        drift.append("owned by another tenant")
    if existing.status != JobStatus.DEAD_LETTER.value:
        drift.append(
            f"status is {existing.status!r}, not "
            f"{JobStatus.DEAD_LETTER.value!r}"
        )
    if existing.remediation_hint != hint:
        drift.append(
            f"remediation_hint is {existing.remediation_hint!r}, not the "
            f"declared {hint!r}"
        )
    if drift:
        raise PoisonMessageFixtureNameInUseError(
            f"fixture_name {fixture_name!r} is already in use by job "
            f"{existing.id} and no longer matches the declared fixture "
            f"({'; '.join(drift)}). Pick a different fixture_name or "
            "reset the environment; this hook never rewrites existing "
            "rows. Nothing was published to Kafka."
        )
