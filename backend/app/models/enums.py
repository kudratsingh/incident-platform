"""The fixed vocabularies the whole platform shares: roles, job types, job and
saga states, and the coarse DLQ category the agent routes on."""

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


# The statuses a job never leaves under its own power; `FAILED` is absent because
# the retry cycle re-enters from it. Each one has a Kafka topic to announce on
# (`job.cancelled` since WO-R2-113), pinned by `test_job_cancelled_topic_wiring.py`.
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
    """Coarse DLQ category the agent routes on, persisted on `jobs.remediation_hint`."""

    # "poison" was never replay_safe: a schema violation always fails (WO-R2-166).
    REPLAY_SAFE = "replay_safe"          # transient fault — replay OK
    WAIT_AND_REPLAY = "wait_and_replay"  # external dep down — retry later
    HUMAN_REQUIRED = "human_required"    # persistent bug — do NOT replay
