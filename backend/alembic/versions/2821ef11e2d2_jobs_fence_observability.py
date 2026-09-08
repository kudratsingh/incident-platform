"""jobs.fenced_at / jobs.fenced_by — a fence is an observable action (WO-R2-158)

Revision ID: 2821ef11e2d2
Revises: e2a9c4f70b31
Create Date: 2026-09-07 18:44:09.780617

Records WHEN an operator fenced a dead-letter row with
`mark_dlq_permanent`, and WHICH principal did it.

`remediation_hint` could not answer either question. The value
`human_required` is byte-identical whether the LLM triage consumer
classified the row or a person deliberately pulled it out of the replay
guardrails, so the only trace of an operator's fence was an `audit_logs`
row — and there was not always even one of those. Confirmed live
2026-09-08: `mark_dlq_permanent` on a row already `human_required` took an
`already_marked` early return and wrote nothing at all. A fence was
therefore unverifiable on the row it fenced, and on an already-classified
row it was indistinguishable from doing nothing. An eval that grades the
fence graded a no-op.

`fenced_at` is stamped on every mark from here on, including a re-mark of
a row that was already `human_required` — an idempotent re-fence is still
an operator action, and `fenced_at` is the field a caller verifies the
fence on. Clock: the platform process's own, aware UTC, at the moment of
the call. Deliberately not `completed_at`/`dead_lettered_at` (when the job
died) or `created_at` (when it was submitted); the three can be days
apart, which is why the column exists rather than being inferred.

`fenced_by` holds `"{principal_type}:{principal_id}"` — e.g.
`service_account:0f9a…` — as a plain string with no FK. Self-describing
rather than a bare UUID because the same id space is `users.id` or
`service_accounts.id` depending on the discriminator, and a column that
cannot say which is precisely the shape
[ADR 0007](../../../docs/ADR/0007-machine-principal-scope-model.md)
rejected for `audit_logs`. No FK for the same reason `audit_logs.principal_id`
has none: the fence record has to outlive the principal.

Both are NULLABLE with NULL meaning "nobody has fenced this row", and both
are episode-scoped exactly as `remediation_hint` (e3f2c6b91d84, R2-23) and
`dead_lettered_by` (c9a3e5d70b12, F2-16) are — `JobService.replay_job`
clears all four together. A fence timestamp surviving a replay next to a
NULL hint would reintroduce the incoherence these columns were added to
remove.

No index on either. `fenced_at` is read per row on an already-filtered DLQ
page and is never a filter predicate; `fenced_by` is a forensic read
reached through the job, and the audit trail (joined by
`audit_logs.resource_id`) remains the queryable record of who did what.

No backfill. Historical fences are reconstructible from
`audit_logs.action = 'job.marked_permanent'`, which is ground truth and
untouched by this migration; guessing a timestamp per row from it would
put a fabricated value in a column whose whole purpose is to be trusted.
Pre-migration rows read NULL, which is honest: this platform did not
record the fact.

No grant needed for the `incident_app` runtime role (b8e4a1c92f35): a
table-level GRANT in Postgres covers columns added later.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2821ef11e2d2"
down_revision: str | Sequence[str] | None = "e2a9c4f70b31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("fenced_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("fenced_by", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "fenced_by")
    op.drop_column("jobs", "fenced_at")
