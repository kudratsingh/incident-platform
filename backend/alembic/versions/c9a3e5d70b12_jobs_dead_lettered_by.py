"""jobs.dead_lettered_by (F2-16)

Revision ID: c9a3e5d70b12
Revises: b8e4a1c92f35
Create Date: 2026-08-09 15:00:00.000000

Records WHICH mechanism forced a job into the DLQ, when that mechanism
was not the default one.

The admin DLQ table used to infer this arithmetically — `retry_count <
max_retries` was read as "something cut the retries short, so it must
have been the LLM retry policy". That inference is wrong for at least
two populations: saga compensation jobs, which dead-letter immediately
at retry_count=0 because they have no registered processor, and the
dispatcher's force-dead-letter safety net. Both were badged as
LLM-driven even with LLM features switched off entirely.

The true signal existed only in `audit_logs.extra_data`. The DLQ tab
lists pages of jobs, so reading it from there means a per-row audit
join on a hot admin path — hence a column.

Values (app-layer, no CHECK constraint, same rationale as
remediation_hint: new mechanisms must not need a schema change):
  - llm_retry_policy — the LLM-guided retry policy returned
                       `dead_letter_now` while deterministic retries
                       remained (workers/dispatcher.py).

NULL means "the default mechanism" — retries exhausted, no processor
registered, or the safety net. No index: the column is read per row on
an already-filtered DLQ page, never used as a filter predicate.

No backfill. The historical attribution lives in audit_logs and is not
reconstructible per row without the join this column exists to avoid;
pre-migration rows render unbadged, which is more honest than the false
positives they render today.

No grant needed for the incident_app runtime role (b8e4a1c92f35): a
table-level GRANT in Postgres covers columns added later.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9a3e5d70b12"
down_revision: str | Sequence[str] | None = "b8e4a1c92f35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("dead_lettered_by", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "dead_lettered_by")
