"""jobs.requeued_at + jobs.heartbeat_at (WO-R2-28)

Revision ID: b1f39d7c2a84
Revises: e5c93b7a2d18
Create Date: 2026-08-30 14:20:00.000000

Two columns for the same defect in two places: a dispatcher sweep acting on
rows it does not own and cannot tell it has already acted on.

* `requeued_at` — when the stale-PENDING backstop last re-published this
  job. The backstop's predicate was `status='pending' AND updated_at <
  cutoff`, and re-publishing touched nothing, so a job stayed inside its own
  predicate and was re-published every 60s for as long as the dispatcher was
  behind. Stamping this column inside the same transaction as the outbox
  insert takes the row out of the predicate for one cutoff window, which
  turns "every sweep interval, forever" into "at most once per window".

  It is a new column rather than a bump of `updated_at` on purpose.
  `updated_at` is the staleness signal itself ("time since last progress")
  and is rendered in the DLQ list and trace views; overloading it would both
  hide how long a job has really been stuck from operators and make the
  backstop's own write indistinguishable from real progress.

* `heartbeat_at` — when the worker executing this job last checked in. The
  stale-RUNNING crash sweep excluded only the *local* process's in-flight
  ids, which is a set that exists in one replica's memory, so with more than
  one replica (or a rolling-deploy overlap) one replica dead-lettered
  another replica's still-executing job and fired a real `job.dlq` for it.
  A column every replica can read is the cross-replica form of the same
  question, and it is also what the recovery write compares against so a
  lease renewed between the scan and the write refuses the write.

Both nullable, and NULL is meaningful in both:

* `requeued_at IS NULL` — never re-published. Eligible, which is right:
  every existing PENDING row should still be recoverable after this
  migration.
* `heartbeat_at IS NULL` — nobody has checked in. Treated as stale, which is
  also right: a job orphaned by a crash before its first renewal is exactly
  what the sweep exists to reclaim.

The upgrade backfills `heartbeat_at = NOW()` for rows currently RUNNING.
Without it, every job in flight across the deploy would read as
"never checked in" the moment the migration lands, and a new-version replica
could reap an old-version replica's live work — the very race this revision
exists to close. The backfill buys one lease TTL, by which time the
renewal loop has taken over. A genuinely orphaned row is not lost by it,
only reclaimed one lease TTL later than it otherwise would have been.

No index is added. Both columns are only ever read alongside predicates that
already narrow to a status (`status='pending'`, `status='running'`), which
`ix_jobs_status` covers; a second index on a column written every renewal
interval would cost more in write amplification than it saves on a scan
capped at 100 rows.

No grant needed for the incident_app runtime role (b8e4a1c92f35): a
table-level GRANT in Postgres covers columns added later.

Downgrade drops both columns. It is lossy in the way that matters: the
sweeps revert to their pre-fix behaviour on the next deploy of matching
code, so a dispatcher that is behind will re-publish stale PENDING jobs
every 60s again and multi-replica RUNNING recovery goes back to being
local-only. Nothing needs to be captured first — neither column carries
state anything else reads.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1f39d7c2a84"
down_revision: str | Sequence[str] | None = "e5c93b7a2d18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("requeued_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Grant every in-flight job one lease window across the deploy — see the
    # module docstring. Deliberately not `updated_at`-preserving: this runs
    # once, offline, and `jobs.updated_at` has no onupdate at the SQL level
    # (it is a SQLAlchemy-side default), so a bare UPDATE leaves it alone.
    op.execute(
        sa.text(
            "UPDATE jobs SET heartbeat_at = NOW() WHERE status = 'running'"
        )
    )


def downgrade() -> None:
    op.drop_column("jobs", "heartbeat_at")
    op.drop_column("jobs", "requeued_at")
