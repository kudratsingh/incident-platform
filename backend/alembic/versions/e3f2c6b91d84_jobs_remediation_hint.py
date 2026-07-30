"""jobs.remediation_hint

Revision ID: e3f2c6b91d84
Revises: d1a8f4e73c62
Create Date: 2026-07-30 09:00:00.000000

Adds `jobs.remediation_hint` — a coarse categorization the
incident-commander agent uses to decide whether to auto-replay,
wait-and-replay, or escalate to a human.

Values (enforced at the app layer, not by a CHECK constraint so
new values can be added without a schema change):
  - replay_safe        — transient / poison; the DLQ entry can be
                         replayed once the underlying consumer bug
                         is fixed. Example: schema-violation payload.
  - wait_and_replay    — external dependency was transiently down;
                         replay once it recovers. Example: SMTP or
                         Stripe timeout.
  - human_required     — persistent bug in the job or its input;
                         replaying will fail the same way. Example:
                         bad data row that trips a ValueError.

Nullable. Only DLQ entries get a value today; the LLM triage service
populates it going forward, and the seed script + chaos hooks set it
directly for eval fixtures. Non-DLQ jobs stay null.

Index on `(status, remediation_hint)` — the exact scan
`replay_dlq_by_category` performs.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3f2c6b91d84"
down_revision: str | Sequence[str] | None = "d1a8f4e73c62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("remediation_hint", sa.String(32), nullable=True),
    )
    op.create_index(
        "ix_jobs_status_remediation_hint",
        "jobs",
        ["status", "remediation_hint"],
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_status_remediation_hint", table_name="jobs")
    op.drop_column("jobs", "remediation_hint")
