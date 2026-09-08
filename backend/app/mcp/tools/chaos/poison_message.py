"""
`poison_message` — publish a schema-invalid payload to a Kafka topic AND
drop a matching dead-letter row that is **not** replay-safe.

Two effects (both observable via `list_dlq_messages` +
`get_consumer_lag`):

1. **Kafka side.** Sends a schema-invalid payload; the target consumer's
   `_process_one` catches `SchemaValidationError`, logs, commits, and
   moves on. Bypasses `publish_raw` (which validates) using an inline
   short-lived aiokafka producer. Unchanged — the injection is real.

2. **DLQ side.** Writes a synthetic `jobs` row with
   `status=dead_letter`, a schema-validation error string from
   `app.lab.dlq_failure_stories`, and a `remediation_hint` the caller
   declares: `unclassified` (the default, `NULL` in the column) or
   `human_required`. That row is how the agent's remediation loop sees
   the poisoning — the platform's real consumers don't route schema
   errors to DLQ (they log-and-drop), so without this synthetic row the
   agent would see no downstream effect.

## Why the row is no longer `replay_safe` (WO-R2-166)

Until v0.6.2 this hook stamped `remediation_hint=replay_safe`. That was
wrong in the way that costs a paid eval run, and it was wrong twice over.

The first version paired `replay_safe` with the schema-validation text,
which live run `efdc3b2a9864` proved unwinnable: the agent read "payload
missing required field", correctly judged that every replay fails on the
same field, and escalated — graded as a failure. WO-R2-146 repaired the
*pair* by moving the text, so the row read `UpstreamTimeout …` instead.
It passed the coherence screen and it was still a lie: this hook injects
a schema violation and nothing else, so a row claiming a transient
timeout described a fault that never happened, and invited a replay of a
payload no replay can fix.

Both repairs picked the wrong half. The truth this hook has to tell is
the one in its own name: **a poisoned message is not safe to replay.** So
the hint moves instead of the text. The row now says what was actually
injected, and says nothing that authorises a replay.

`unclassified` is the default because that is the honest arrival state: a
freshly poisoned message has been classified by nobody. LLM triage is off
by default on this platform, so an organically dead-lettered job's
`remediation_hint` is NULL, and a null hint under a permanent-fault text
is the coherent pair `app.lab.dlq_failure_stories` admits. `human_required`
is offered for a scenario that wants the row *already* categorised, so
`replay_dlq_by_category` refuses it on sight and the agent's
escalate-not-replay branch is reachable without a triage step.

What a caller cannot ask for is `replay_safe`, on any argument. A scenario
that deliberately wants a mislabelled row calls
`create_mislabeled_dlq_job`, which exists precisely so that this hook
never has to be able to lie.

## Deterministic ids

The row's id is `uuid5(namespace, f"{tenant_id}:{fixture_name}")` — the
same convention as `create_bad_data_job` and `create_stuck_dag`, with its
own namespace, so the id cannot collide with a bad-data fixture, a chain
node or a boot-seeded row even under an identical `fixture_name`. A
scenario can pin the id in YAML before the hook runs, given the tenant it
will run as; it has to, because the graders assert *which* row the agent
acted on and a random id cannot be named in a claim written before the
run (commander cmd #187).

Re-invoking with the same `fixture_name` is idempotent on the DLQ row
while that row still matches what the call declares; once it has drifted
— someone fenced it, a replay moved it out of `dead_letter`, or this call
declares a different hint — the hook refuses rather than rewriting
history. The refusal happens **before** the Kafka send, so a refused call
has no side effect at all.

The Kafka half is not idempotent and does not pretend to be: every
accepted call really does put another schema-invalid message on the
topic, including a call that finds its row already present. That is the
tool's primary verb; the description says so.

## Disposal

The row is tagged `payload.seeded_fixture = true`, so the reset sweep
(`scripts/reset_eval_state.py::_delete_seeded_dlq_fixtures`) DELETEs it —
a change of disposal class from the pre-v0.6.3 shape, where a randomly
idded row marked only `chaos_fixture` was *cancelled* by
`_sweep_nonfixture_dlq` on the grounds that it stood in for something
that happened to a real user's job. A row with a scenario-pinned id,
declared by name, is not that: it is scaffolding, the same way
`create_bad_data_job`'s and `create_stuck_dag`'s rows are, and leaving a
`cancelled` copy behind per run is litter rather than history (ADR 0012
rule 2). `chaos_fixture` stays in the payload beside the marker so the
row's provenance is still readable.

Requires `chaos:invoke`. Registered only when `CHAOS_ENABLED=true`.
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

# One spelling of "write NULL into remediation_hint" and one resolver for
# it across the declared-fixture hooks, imported rather than restated —
# same reason `create_bad_data_job` imports `_validated_hint` from
# `seed_dlq_messages` instead of re-deriving it. `_ensure_chaos_owner` is
# shared for a harder reason: the reset's `_delete_chaos_owner_users`
# sweep recognises that user by email prefix, so a hook that lazy-created
# its own would leave rows the reset cannot reach.
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


class PoisonMessageFixtureNameInUseError(AppError):
    """The declared `fixture_name` names a row that no longer matches the
    call. Same 409 shape and same reasoning as
    `create_bad_data_job`'s `bad_data_fixture_name_in_use`: a drifted row
    is evidence, and re-manufacturing it would hand the next run a
    pre-remediated world and grade it clean."""

    status_code = 409
    error_code = "poison_fixture_name_in_use"


logger = get_logger(__name__)

# uuid5 namespace for poisoned-message fixture ids. Fixed and documented
# so a scenario can precompute the id it pins:
# uuid5(ns, f"{tenant_id}:{fixture_name}").
# Distinct from the eval seed's namespace (aaaaaaaa-…), `create_stuck_dag`'s
# (cccccccc-…), `create_bad_data_job`'s (dddddddd-bad0-…) and
# `create_mislabeled_dlq_job`'s (ffffffff-11ed-…), so the same
# `fixture_name` under two hooks is two independent rows rather than a
# primary-key collision. "dead" is the mnemonic: this is the dead-letter
# row that stands in for a poisoned message.
_NAMESPACE = uuid.UUID("eeeeeeee-dead-4000-8000-000000000000")

# The story each declared hint stamps. Both are schema-violation texts on
# purpose: this hook injects exactly one kind of fault, and the only
# difference between the two rows is whether anybody has classified it.
_STORY_KEY_FOR_HINT: dict[str | None, str] = {
    RemediationHint.HUMAN_REQUIRED.value: "schema_missing_field",
    None: "unclassified_schema_missing_field",
}


def fixture_id(tenant_id: uuid.UUID, fixture_name: str) -> uuid.UUID:
    """The row's deterministic id.

    Exported so a test — or a scenario's own precompute — derives it the
    same way the hook does instead of transcribing the recipe. Per-tenant
    for the same reason `create_bad_data_job`'s is: the idempotency probe
    below runs on the RLS-scoped MCP session, so a row another tenant
    created under the same name would be invisible to it and the INSERT
    would collide on the primary key — a 500 where the contract promises
    a 409.
    """
    return uuid.uuid5(_NAMESPACE, f"{tenant_id}:{fixture_name}")


def _dlq_error_for_topic(topic: str, hint: str | None) -> str:
    """The error text on the synthetic DLQ row this hook writes.

    Module level, and separate from the handler, so the coherence table
    test can check the (hint, text) pairs this hook produces without a
    broker (`tests/unit/test_dlq_text_coherence.py`).

    The text is the schema-validation story for the declared hint —
    a permanent fault, which is what this hook actually injects. It is
    coherent under both hints the input model admits: `human_required`
    requires a permanent marker, and a null hint admits one (a
    classification is not a symptom; see the asymmetric rule in
    `app.lab.dlq_failure_stories`).

    The row keeps naming its topic and this hook so a human sweeping the
    DLQ can trace it back, and so the Kafka half and the DLQ half of one
    invocation can be joined by eye.
    """
    base = story(_STORY_KEY_FOR_HINT[hint]).error_message
    return f"{base} (chaos poison_message on topic '{topic}')"


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
    # Deliberately a *subset* of `RemediationHint` plus the sentinel, and
    # deliberately missing `replay_safe`: this hook injects a schema
    # violation, and a schema violation is never safe to replay. A
    # scenario that wants a row whose hint contradicts its text calls
    # `create_mislabeled_dlq_job`, which says so in its name.
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

    # Primary-key read, RLS-scoped like every other tool call, and BEFORE
    # the producer: a call that is going to be refused must not have
    # poisoned a topic on its way to the refusal.
    existing = (
        await ctx.db.execute(select(Job).where(Job.id == job_id))
    ).scalar_one_or_none()
    if existing is not None:
        _assert_matches(inp.fixture_name, existing, hint, tenant_id)

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

    if existing is not None:
        # Idempotent repeat: the row this call declares is already there
        # and still matches. The topic got another poisoned message, which
        # is the half of this tool that is a verb rather than a fixture.
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

    Same rule and same wording as `create_bad_data_job._assert_matches`:
    a repeat that finds the row still `dead_letter`, in the caller's
    tenant, carrying exactly the hint this call declares is a no-op on the
    row. Anything else is refused, because re-manufacturing would mean
    rewriting a row that is now evidence:

      * `remediation_hint` moved — somebody fenced it, which on an
        `unclassified` poisoned row is the very action a scenario seeds it
        to measure. Silently reporting `created=False` here would hand the
        next run a pre-fenced world and grade it clean.
      * `status` moved — a replay took the row out of `dead_letter`.
      * this call declares a different hint than the stored row carries,
        so returning the row would report a fixture the caller did not
        ask for.
    """
    drift: list[str] = []
    if existing.tenant_id != tenant_id:
        # Unreachable while ids are tenant-derived; kept because it is the
        # invariant the derivation exists to guarantee, and a non-RLS
        # session (a script, a superuser) is the one caller that could
        # still see a foreign row here.
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
