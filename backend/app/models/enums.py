from enum import StrEnum


class UserRole(StrEnum):
    USER = "user"
    SUPPORT = "support"
    ADMIN = "admin"


class JobType(StrEnum):
    CSV_UPLOAD = "csv_upload"
    REPORT_GEN = "report_gen"
    BULK_API_SYNC = "bulk_api_sync"
    DOC_ANALYSIS = "doc_analysis"


class JobStatus(StrEnum):
    WAITING = "waiting"            # has unmet dependencies — not dispatched yet
    PENDING = "pending"            # ready to run, in the queue / Kafka log
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"        # saga rollback / dependency parent failed


# The statuses a job never leaves under its own power (a DLQ replay is an
# operator re-entering it deliberately). `FAILED` is deliberately absent: the
# retry cycle re-enters from it, so a `failed` row is still in flight.
#
# Every status here has a Kafka topic to announce on — `job.completed`,
# `job.dlq` and, since WO-R2-113, `job.cancelled` — so
# `JobRepository.update_status` emits an outbox row for all three in the same
# transaction as the status write. `test_job_cancelled_topic_wiring.py` pins
# that equality, so a terminal status added here without a topic fails a test
# instead of stopping jobs silently the way `CANCELLED` used to.
TERMINAL_JOB_STATUSES: frozenset[str] = frozenset(
    {JobStatus.COMPLETED, JobStatus.DEAD_LETTER, JobStatus.CANCELLED}
)


class SagaStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    COMPENSATING = "compensating"  # compensation jobs in flight
    COMPENSATED = "compensated"    # all compensation jobs settled


class RemediationHint(StrEnum):
    """Coarse DLQ categorization the agent reads to pick a remediation
    strategy. Persisted on `jobs.remediation_hint`. Never inferred
    from the raw error message at read time — treat the column as the
    canonical source. Set by:
      - the LLM triage service (Phase 10) when it classifies a DLQ —
        but ONLY when `LLM_TRIAGE_ENABLED` is on, and it is off by
        default (ADR 0005). With triage off nothing categorises an
        organically dead-lettered job, and every value in the column
        comes from one of the three writers below. Said out loud
        because this list previously named triage as a setter that
        wrote nothing at all: it persisted a `job_triages` row and
        never touched this column (R2-24).
      - the eval seed script (see scripts/seed_eval_fixtures.py)
      - chaos hooks that produce DLQ entries (seed_dlq_messages,
        create_stuck_dag, create_bad_data_job, poison_message,
        create_mislabeled_dlq_job) — every one of them stamping a value
        that agrees with the row's error text, checked by
        `tests/unit/test_dlq_text_coherence.py`. The single exception is
        `create_mislabeled_dlq_job`, whose whole purpose is a row whose
        hint its text contradicts; see
        `app.lab.dlq_failure_stories`
      - the `mark_dlq_permanent` Tier-1 tool (agent-driven)

    Scoped to one dead-letter episode: cleared on replay (R2-23), so a
    job that dead-letters again is categorised afresh. NULL means "not
    categorised", which the tools read as unknown — explicitly NOT as
    replay-safe.
    """

    # "poison" used to be listed here as a replay_safe cause. It never
    # was one: a message that fails schema validation fails identically on
    # every attempt (WO-R2-166). `poison_message`'s dead-letter row is
    # unclassified or `human_required` now, never this value.
    REPLAY_SAFE = "replay_safe"          # transient fault — replay OK
    WAIT_AND_REPLAY = "wait_and_replay"  # external dep down — retry later
    HUMAN_REQUIRED = "human_required"    # persistent bug — do NOT replay
