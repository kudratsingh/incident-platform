"""
Two rules the platform evaluates on its own clock, so the platform pages itself (ADR 0039).

Before this, the only non-chaos producer of an `Alert` row was the SLO fast-burn evaluator
(`services/slo.py`), which answers a question about a 24-hour error budget. Nothing looked
at the two conditions an operator actually watches minute to minute — a consumer group
falling behind, and a dead-letter queue filling up — so the demo's agent was handed an
alert its own scenario file had written while the platform's alert stream read the same
three seeded fixtures before, during and after the fault.

Three decisions worth keeping in view:

**An episode raises once.** The dedup identity is `rule:<fingerprint>:<subject>:<ordinal>`
where the ordinal counts the episodes this rule has already had for that subject, and
`alerts.dedup_key` is unique per tenant. So a sustained breach pages once however many
ticks it spans, two replicas evaluating the same tick cannot both insert (the constraint
settles it, exactly as `_fast_burn_dedup_key` does), and a *new* episode after a recovery
gets a new key instead of being suppressed forever by the old one. No Redis marker: state
the reset would have to sweep, and forgetting to sweep it would silence the next take.

**A recorded constant is never a breach.** Seven consumer groups report a lag the seed
script wrote once (500 … 100,000) and nothing refreshes them; a rule reading those values
would page six times on a healthy world and never resolve, because nothing can bring a
constant down. So the lag rule reads the latest MEASURED sample, which only the
continuously-refreshed group has. Absence is not recovery either: a window that stopped
being written leaves an open episode open, because `get_consumer_lag`'s own rule is that
unknown is not zero.

**The alert says what the platform can see.** The DLQ rule names a category only when the
rows above the baseline carry one, and names the unclassified slice when they do not. It
never reaches for the lab's marker to work out which rows are "real" — the rows above the
baseline are the newest ones, by the same clock and the same order
(`JobSort.DEAD_LETTERED_AT`) the agent's own `list_dlq_messages` page uses.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config import get_settings
from app.core.consumer_lag import LagSampleReading, read_lag, source_for
from app.core.logging import get_logger
from app.models.alert import SEVERITY_CRITICAL, Alert
from app.models.audit import PRINCIPAL_TYPE_SERVICE_ACCOUNT
from app.models.enums import JobStatus
from app.models.job import Job
from app.models.tenant import DEFAULT_TENANT_ID
from app.repositories.alert import AlertRepository
from app.repositories.audit import AuditRepository
from app.services.alerts import AlertService
from app.utils.post_commit import run_post_commit
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = get_logger(__name__)

#: The fingerprints the commander's scenario files already use for these two conditions,
#: so an alert the platform raises and an alert a YAML synthesises are the same incident.
CONSUMER_STALLED_FINGERPRINT = "consumer_stalled"
DLQ_DEPTH_FINGERPRINT = "dlq_depth_warning"

#: `Alert.source` is freeform (`slo:job_completion`, `dlq:threshold`). These two follow the
#: fixture vocabulary's prefixes — `kafka` and `dlq` — with a suffix naming the reading.
CONSUMER_LAG_ALERT_SOURCE = "kafka:consumer_lag"
DLQ_DEPTH_ALERT_SOURCE = "dlq:threshold"

#: The whole-tenant DLQ total has no subject of its own; the tenant is already the scope.
DLQ_SUBJECT = "total"

#: One audit row per transition, under their own prefix. NOT withheld from the agent:
#: `hidden_audit_action_prefixes` hides the lab (`chaos.`, `lab.`) and the responder's own
#: report stream, and an alert is the one thing the agent is *supposed* to know about — it
#: is what it was paged with. Nothing in these rows names a mechanism (ADR 0012 rule 1).
ALERT_RAISED_ACTION = "alert.raised"
ALERT_RESOLVED_ACTION = "alert.resolved"
ALERT_ACTION_PREFIX = "alert."
ALERT_RESOURCE_TYPE = "alert"

#: `rule:<fingerprint>:<subject>:<ordinal>` — well inside `dedup_key`'s 128 characters
#: even with a UUID subject.
_DEDUP_NAMESPACE = "rule"


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    """What one evaluation did. Empty is the normal answer on a healthy world."""

    raised: list[uuid.UUID] = field(default_factory=list)
    resolved: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Episode:
    """One rule's judgement about one subject on one tick."""

    fingerprint: str
    subject: str
    tenant_id: uuid.UUID
    source: str
    title: str
    summary: str
    payload: dict[str, Any]


def dedup_prefix(fingerprint: str, subject: str) -> str:
    return f"{_DEDUP_NAMESPACE}:{fingerprint}:{subject}:"


def dedup_key(fingerprint: str, subject: str, ordinal: int) -> str:
    return f"{dedup_prefix(fingerprint, subject)}{ordinal}"


# ---------------------------------------------------------------------------
# Episode bookkeeping
# ---------------------------------------------------------------------------


async def _open_alert(
    session: AsyncSession, tenant_id: uuid.UUID, prefix: str
) -> Alert | None:
    """The unresolved alert of this episode series, if one is open."""
    result = await session.execute(
        select(Alert)
        .where(
            Alert.tenant_id == tenant_id,
            Alert.resolved_at.is_(None),
            # `autoescape` because a fingerprint contains `_`, which LIKE reads as a
            # single-character wildcard — a prefix test has to mean a prefix test.
            Alert.dedup_key.startswith(prefix, autoescape=True),
        )
        .order_by(Alert.fired_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def _episodes_so_far(
    session: AsyncSession, tenant_id: uuid.UUID, prefix: str
) -> int:
    """How many episodes this series has had, resolved ones included.

    The next episode's ordinal. Two replicas compute the same number from the same rows,
    so they build the same `dedup_key` and the unique constraint picks one — which is the
    whole reason the ordinal is counted rather than allocated.
    """
    total = (
        await session.execute(
            select(func.count())
            .select_from(Alert)
            .where(
                Alert.tenant_id == tenant_id,
                Alert.dedup_key.startswith(prefix, autoescape=True),
            )
        )
    ).scalar_one()
    return int(total)


async def _record_transition(
    session: AsyncSession,
    *,
    action: str,
    alert: Alert,
    fingerprint: str,
    subject: str,
    summary: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """One audit row for a raise or a resolve, in the alert's own transaction.

    The opposite error contract to `record_tool_invocation`, and for `record_world_reset`'s
    reason: there is no response to protect here, and an alert the audit stream does not
    carry is an alert the console cannot draw a "paged" station from. So this raises, the
    caller's transaction unwinds, and the rule tries again on the next pass.

    `principal_type` is `service_account` with a null `principal_id`: no human and no
    account did this — the platform's own loop did, and ADR 0007 makes the id nullable
    exactly so an actor without one still gets a row.
    """
    payload: dict[str, Any] = {
        "alert_id": str(alert.id),
        "fingerprint": fingerprint,
        "subject": subject,
        "source": alert.source,
        "severity": alert.severity,
        "summary": summary,
        "dedup_key": alert.dedup_key,
    }
    payload.update(extra or {})
    await AuditRepository(session).log(
        action,
        tenant_id=alert.tenant_id,
        principal_type=PRINCIPAL_TYPE_SERVICE_ACCOUNT,
        principal_id=None,
        user_id=None,
        resource_type=ALERT_RESOURCE_TYPE,
        resource_id=str(alert.id),
        extra_data=payload,
    )


async def _raise_episode(
    session_factory: async_sessionmaker[AsyncSession], episode: _Episode
) -> uuid.UUID | None:
    """Open one episode, or return None because it is already open (or lost the race).

    Its own session per episode, so one rule's conflict cannot roll back another's write —
    `_raise_fast_burn_alert`'s shape, including the post-commit drain sitting AFTER the
    transaction so a suppressed alert is never delivered.
    """
    prefix = dedup_prefix(episode.fingerprint, episode.subject)
    try:
        async with session_factory() as session:
            async with session.begin():
                if await _open_alert(session, episode.tenant_id, prefix) is not None:
                    return None
                ordinal = await _episodes_so_far(session, episode.tenant_id, prefix)
                service = AlertService(AlertRepository(session))
                alert = await service.create_alert(
                    tenant_id=episode.tenant_id,
                    severity=SEVERITY_CRITICAL,
                    source=episode.source,
                    title=episode.title,
                    description=episode.summary,
                    extra_data=episode.payload,
                    dedup_key=dedup_key(
                        episode.fingerprint, episode.subject, ordinal
                    ),
                )
                await _record_transition(
                    session,
                    action=ALERT_RAISED_ACTION,
                    alert=alert,
                    fingerprint=episode.fingerprint,
                    subject=episode.subject,
                    summary=episode.summary,
                )
            await run_post_commit(session)
            logger.warning(
                "platform alert rule raised",
                extra={
                    "fingerprint": episode.fingerprint,
                    "subject": episode.subject,
                    "alert_id": str(alert.id),
                },
            )
            return alert.id
    except IntegrityError:
        # Another replica won this episode's key. De-duplication working, not a failure.
        logger.debug(
            "alert rule episode already raised",
            extra={"fingerprint": episode.fingerprint, "subject": episode.subject},
        )
        return None


async def _resolve_episode(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    fingerprint: str,
    subject: str,
    reason: str,
) -> uuid.UUID | None:
    """Close the open episode of one series, if there is one. Resolved, never deleted."""
    prefix = dedup_prefix(fingerprint, subject)
    async with session_factory() as session:
        async with session.begin():
            alert = await _open_alert(session, tenant_id, prefix)
            if alert is None:
                return None
            alert.resolved_at = datetime.now(UTC)
            summary = f"{subject}: {reason}"
            await _record_transition(
                session,
                action=ALERT_RESOLVED_ACTION,
                alert=alert,
                fingerprint=fingerprint,
                subject=subject,
                summary=summary,
                extra={"resolved_reason": reason},
            )
            alert_id = alert.id
    logger.info(
        "platform alert rule resolved",
        extra={
            "fingerprint": fingerprint,
            "subject": subject,
            "alert_id": str(alert_id),
        },
    )
    return alert_id


# ---------------------------------------------------------------------------
# Rule 1 — consumer_stalled
# ---------------------------------------------------------------------------


def _lag_summary(group: str, sample: LagSampleReading, threshold: int) -> str:
    return (
        f"{group} is {sample.lag} messages behind (threshold {threshold}); "
        f"the group holds its assignment and keeps reporting, so the backlog is "
        f"real. Measured at {sample.measured_at.isoformat()}."
    )


def _lag_payload(
    group: str, sample: LagSampleReading, threshold: int, summary: str
) -> dict[str, Any]:
    """The alert body, in the shape a responder's alert ingress already accepts.

    `consumer_group` AND `group` carry the same value because the receiver reads whichever
    it finds, and `severity` / `source` are repeated from the row's own columns so this one
    dict is a complete alert rather than half of one — a poller taking the payload verbatim
    should not have to stitch two levels of a webhook body back together.
    """
    return {
        "fingerprint": CONSUMER_STALLED_FINGERPRINT,
        "severity": SEVERITY_CRITICAL,
        "source": CONSUMER_LAG_ALERT_SOURCE,
        "consumer_group": group,
        "group": group,
        "lag": sample.lag,
        "measured_at": sample.measured_at.isoformat(),
        "threshold": threshold,
        "summary": summary,
    }


async def _evaluate_consumer_lag(
    session_factory: async_sessionmaker[AsyncSession], redis: Any, threshold: int
) -> RuleOutcome:
    """The lag rule, over every group whose lag is measured rather than recorded.

    The tenant is the platform tenant, like a fast-burn alert: Kafka lag is not a property
    of a customer, and pinning it on one would be a lie.
    """
    raised: list[uuid.UUID] = []
    resolved: list[uuid.UUID] = []
    for group in _measured_groups():
        reading = await read_lag(redis, group)
        if not reading.recent_samples:
            # No measurement this pass: neither a breach nor a recovery. An open episode
            # stays open — unknown is not zero.
            continue
        sample = reading.recent_samples[0]
        if sample.lag >= threshold:
            summary = _lag_summary(group, sample, threshold)
            alert_id = await _raise_episode(
                session_factory,
                _Episode(
                    fingerprint=CONSUMER_STALLED_FINGERPRINT,
                    subject=group,
                    tenant_id=DEFAULT_TENANT_ID,
                    source=CONSUMER_LAG_ALERT_SOURCE,
                    title=f"Consumer lag on {group}: {sample.lag} messages behind",
                    summary=summary,
                    payload=_lag_payload(group, sample, threshold, summary),
                ),
            )
            if alert_id is not None:
                raised.append(alert_id)
            continue
        alert_id = await _resolve_episode(
            session_factory,
            tenant_id=DEFAULT_TENANT_ID,
            fingerprint=CONSUMER_STALLED_FINGERPRINT,
            subject=group,
            reason=f"lag measured at {sample.lag}, below the threshold of {threshold}",
        )
        if alert_id is not None:
            resolved.append(alert_id)
    return RuleOutcome(raised=raised, resolved=resolved)


def _measured_groups() -> tuple[str, ...]:
    """The groups whose lag is a measurement. Today exactly the one with a writer.

    Derived from `source_for` rather than naming the group, so a second continuously
    refreshed group is covered by the rule the day it gets a writer.
    """
    from app.core.consumer_lag import SEEDED_CONSUMER_GROUPS

    return tuple(g for g in SEEDED_CONSUMER_GROUPS if source_for(g) == "live")


# ---------------------------------------------------------------------------
# Rule 2 — dlq_depth_warning
# ---------------------------------------------------------------------------


def _dlq_summary(depth: int, threshold: int, hint: str | None) -> str:
    slice_words = (
        f" The rows above the baseline are categorised `{hint}`."
        if hint is not None
        else " The rows above the baseline carry no category yet."
    )
    return (
        f"{depth} jobs are in the dead-letter queue (threshold {threshold})."
        f"{slice_words}"
    )


def _dlq_payload(
    depth: int, threshold: int, hint: str | None, summary: str, measured_at: datetime
) -> dict[str, Any]:
    """`remediation_hint` names the category that pushed the depth over; `dlq_scope` is the
    third way a DLQ alert names its subject, and `unclassified` is the only word the
    receiver recognises for it. Exactly one of the two is ever set."""
    return {
        "fingerprint": DLQ_DEPTH_FINGERPRINT,
        "severity": SEVERITY_CRITICAL,
        "source": DLQ_DEPTH_ALERT_SOURCE,
        "dlq_depth": depth,
        "threshold": threshold,
        "remediation_hint": hint,
        "dlq_scope": None if hint is not None else "unclassified",
        "measured_at": measured_at.isoformat(),
        "summary": summary,
    }


async def _dlq_depth_by_tenant(session: AsyncSession) -> dict[uuid.UUID, int]:
    rows = await session.execute(
        select(Job.tenant_id, func.count())
        .where(Job.status == JobStatus.DEAD_LETTER)
        .group_by(Job.tenant_id)
    )
    return {tenant_id: int(count) for tenant_id, count in rows.all()}


async def _dominant_hint_above_baseline(
    session: AsyncSession, tenant_id: uuid.UUID, above: int
) -> str | None:
    """The category of the newest `above` dead-letter rows, when they agree on one.

    Newest by `COALESCE(completed_at, created_at) DESC` — the clock and the order the
    agent's own `list_dlq_messages` page uses (`JobSort.DEAD_LETTERED_AT`), so the alert
    names rows that are actually at the top of the listing it will read. `None` when those
    rows carry no category, or carry more than one: an alert that picked a winner between
    two categories would be naming a slice nobody can act on.
    """
    if above <= 0:
        return None
    rows = (
        (
            await session.execute(
                select(Job.remediation_hint)
                .where(
                    Job.tenant_id == tenant_id,
                    Job.status == JobStatus.DEAD_LETTER,
                )
                .order_by(
                    func.coalesce(Job.completed_at, Job.created_at).desc(),
                    Job.id.desc(),
                )
                .limit(above)
            )
        )
        .scalars()
        .all()
    )
    categories = {hint for hint in rows if hint is not None}
    if len(categories) != 1:
        return None
    return categories.pop()


async def _evaluate_dlq_depth(
    session_factory: async_sessionmaker[AsyncSession], threshold: int
) -> RuleOutcome:
    """The depth rule, per tenant: a dead-letter backlog really is one tenant's."""
    raised: list[uuid.UUID] = []
    resolved: list[uuid.UUID] = []
    async with session_factory() as session:
        depths = await _dlq_depth_by_tenant(session)
        breaching: dict[uuid.UUID, tuple[int, str | None]] = {}
        for tenant_id, depth in depths.items():
            if depth < threshold:
                continue
            hint = await _dominant_hint_above_baseline(
                session, tenant_id, depth - (threshold - 1)
            )
            breaching[tenant_id] = (depth, hint)

    measured_at = datetime.now(UTC)
    for tenant_id, (depth, hint) in breaching.items():
        summary = _dlq_summary(depth, threshold, hint)
        alert_id = await _raise_episode(
            session_factory,
            _Episode(
                fingerprint=DLQ_DEPTH_FINGERPRINT,
                subject=DLQ_SUBJECT,
                tenant_id=tenant_id,
                source=DLQ_DEPTH_ALERT_SOURCE,
                title=f"Dead-letter queue depth {depth} (threshold {threshold})",
                summary=summary,
                payload=_dlq_payload(depth, threshold, hint, summary, measured_at),
            ),
        )
        if alert_id is not None:
            raised.append(alert_id)

    # Resolution needs the tenants with an OPEN episode, not the ones with rows: a queue
    # that drained to zero has no rows left to group by.
    for tenant_id in await _tenants_with_open_episode(
        session_factory, DLQ_DEPTH_FINGERPRINT, DLQ_SUBJECT
    ):
        if tenant_id in breaching:
            continue
        depth = depths.get(tenant_id, 0)
        alert_id = await _resolve_episode(
            session_factory,
            tenant_id=tenant_id,
            fingerprint=DLQ_DEPTH_FINGERPRINT,
            subject=DLQ_SUBJECT,
            reason=f"depth {depth}, below the threshold of {threshold}",
        )
        if alert_id is not None:
            resolved.append(alert_id)
    return RuleOutcome(raised=raised, resolved=resolved)


async def _tenants_with_open_episode(
    session_factory: async_sessionmaker[AsyncSession], fingerprint: str, subject: str
) -> list[uuid.UUID]:
    prefix = dedup_prefix(fingerprint, subject)
    async with session_factory() as session:
        rows = await session.execute(
            select(Alert.tenant_id)
            .where(
                Alert.resolved_at.is_(None),
                Alert.dedup_key.startswith(prefix, autoescape=True),
            )
            .distinct()
        )
        return [row[0] for row in rows.all()]


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def evaluate_alert_rules(
    session_factory: async_sessionmaker[AsyncSession], redis: Any
) -> RuleOutcome:
    """Evaluate both rules once. Called from the metrics pass, on the metrics clock.

    Raises nothing the caller must handle: each rule owns its own session, and a rule that
    fails is logged and does not stop the other. The gate is read here rather than at the
    call site so every caller — loop, reset, test — honours it.
    """
    settings = get_settings()
    if not settings.alert_rules_enabled:
        return RuleOutcome()

    raised: list[uuid.UUID] = []
    resolved: list[uuid.UUID] = []
    for name, coro in (
        (
            CONSUMER_STALLED_FINGERPRINT,
            _evaluate_consumer_lag(
                session_factory, redis, settings.consumer_lag_alert_threshold
            ),
        ),
        (
            DLQ_DEPTH_FINGERPRINT,
            _evaluate_dlq_depth(session_factory, settings.dlq_depth_alert_threshold),
        ),
    ):
        try:
            outcome = await coro
        except Exception as exc:
            logger.error(
                "alert rule evaluation failed",
                extra={"rule": name, "error": str(exc)},
            )
            continue
        raised.extend(outcome.raised)
        resolved.extend(outcome.resolved)
    return RuleOutcome(raised=raised, resolved=resolved)


async def resolve_open_episodes(
    session_factory: async_sessionmaker[AsyncSession], *, reason: str
) -> list[uuid.UUID]:
    """Close every open rule episode, whatever the current reading says.

    What `scripts/reset_eval_state.py` calls on the take boundary. It goes through the same
    resolution path a recovery takes, so the audit stream never holds an `alert.raised`
    with no `alert.resolved` after it — the generic organic-alert sweep beside it stamps
    `resolved_at` and writes no row, which would leave the console's timeline open.
    """
    closed: list[uuid.UUID] = []
    async with session_factory() as session:
        rows = await session.execute(
            select(Alert.tenant_id, Alert.dedup_key).where(
                Alert.resolved_at.is_(None),
                Alert.dedup_key.startswith(f"{_DEDUP_NAMESPACE}:", autoescape=True),
            )
        )
        open_series = rows.all()

    for tenant_id, key in open_series:
        parts = str(key).split(":")
        if len(parts) < 4:
            continue
        fingerprint, subject = parts[1], ":".join(parts[2:-1])
        alert_id = await _resolve_episode(
            session_factory,
            tenant_id=tenant_id,
            fingerprint=fingerprint,
            subject=subject,
            reason=reason,
        )
        if alert_id is not None:
            closed.append(alert_id)
    return closed


__all__ = [
    "ALERT_ACTION_PREFIX",
    "ALERT_RAISED_ACTION",
    "ALERT_RESOLVED_ACTION",
    "ALERT_RESOURCE_TYPE",
    "CONSUMER_LAG_ALERT_SOURCE",
    "CONSUMER_STALLED_FINGERPRINT",
    "DLQ_DEPTH_ALERT_SOURCE",
    "DLQ_DEPTH_FINGERPRINT",
    "DLQ_SUBJECT",
    "RuleOutcome",
    "dedup_key",
    "dedup_prefix",
    "evaluate_alert_rules",
    "resolve_open_episodes",
]
