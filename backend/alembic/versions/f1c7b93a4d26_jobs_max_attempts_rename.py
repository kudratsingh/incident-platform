"""jobs.max_retries → jobs.max_attempts — the cap counts runs (WO-R2-172)

Revision ID: f1c7b93a4d26
Revises: 2821ef11e2d2
Create Date: 2026-09-08 10:12:44.116308

A pure rename. The column keeps its type, its NOT NULL, and every value
in it; no row's behaviour changes on either side of this migration.

The name was the defect. `workers/dispatcher.py` retries while
`retry_count < max_retries`, so a row carrying 3 gets three RUNS — the
original plus two retries — and dead-letters when the third one fails.
The value is a cap on attempts and always has been: the retry message on
that same path prints `attempt {n}/{max}`, and the dead-letter message
says "exhausted after 3 attempts". Only the column said retries, and a
reader who believed it budgeted one run more than the platform gives.

The user's ruling (2026-09-08) was to fix the name and keep the
arithmetic. Renaming rather than dropping and re-adding is the whole
point: a per-job ceiling that a caller set deliberately — the saga
coordinator copies one onto every compensation job — must survive the
release, and `ALTER TABLE … RENAME COLUMN` moves the data with the name
in one statement, with no window in which the column is missing and no
backfill to get wrong.

`downgrade()` renames it straight back, so this is reversible with no
data loss in either direction.

No grant needed for the `incident_app` runtime role (b8e4a1c92f35): a
table-level GRANT in Postgres follows a renamed column.

Deploy note: the app reads the column by name, so a replica running the
previous release cannot see `max_attempts` and one running this release
cannot see `max_retries`. This is a stop-then-migrate-then-start step,
not a rolling one — the same shape every column rename has.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f1c7b93a4d26"
down_revision: str | Sequence[str] | None = "2821ef11e2d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("jobs", "max_retries", new_column_name="max_attempts")


def downgrade() -> None:
    op.alter_column("jobs", "max_attempts", new_column_name="max_retries")
