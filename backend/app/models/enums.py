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
# `COMPLETED` and `DEAD_LETTER` additionally have a Kafka topic to announce on,
# which is why `JobRepository.update_status` emits an outbox row for those two
# and not for `CANCELLED` — there is no `job.cancelled` topic.
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
      - the LLM triage service (Phase 10) when it classifies a DLQ
      - the eval seed script (see scripts/seed_eval_fixtures.py)
      - chaos hooks that produce DLQ entries (poison_message,
        create_bad_data_job)
      - the `mark_dlq_permanent` Tier-1 tool (agent-driven)
    """

    REPLAY_SAFE = "replay_safe"          # transient / poison — replay OK
    WAIT_AND_REPLAY = "wait_and_replay"  # external dep down — retry later
    HUMAN_REQUIRED = "human_required"    # persistent bug — do NOT replay
