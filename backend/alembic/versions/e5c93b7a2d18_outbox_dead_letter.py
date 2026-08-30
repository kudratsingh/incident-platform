"""outbox_events.failed_at + error_message (WO-R2-05)

Revision ID: e5c93b7a2d18
Revises: d7b6c48f1a95
Create Date: 2026-08-30 11:00:00.000000

Gives the outbox relay the failed state ADR 0001 has always documented and
never had. Decision item 3 of that ADR specifies that an unpublishable row
is marked `published_at=NOW, error_message=...` rather than retried
forever; `error_message` did not exist, so neither did the branch.

Two columns, both nullable, no backfill:

* `error_message` — why the row was abandoned. TEXT rather than a bounded
  VARCHAR because the value is an exception string, and the writer
  truncates to 900 chars anyway (`_ERROR_MESSAGE_MAX_CHARS`); a driver
  overflow here would roll back the marking transaction and hand the
  poison row straight back to the relay, which is the exact failure the
  column exists to end.
* `failed_at` — set together with `published_at` when a row is
  dead-lettered. `published_at` alone cannot carry this: the relay needs
  it set so the row leaves `fetch_unpublished`'s window (and the partial
  index on `published_at IS NULL` keeps working untouched), but a row that
  was abandoned must never read back as one that was delivered.
  `published_at IS NOT NULL AND failed_at IS NULL` is a real publish.

Existing rows are correct as NULL/NULL: nothing has been dead-lettered
because nothing could be. The partial index `ix_outbox_events_unpublished`
is deliberately left alone — the new `attempts < cap` predicate filters
within the same `published_at IS NULL` set it already covers.

No grant needed for the incident_app runtime role (b8e4a1c92f35): a
table-level GRANT in Postgres covers columns added later.

Downgrade drops both columns. It is lossy in one specific way worth
stating: any row dead-lettered while this revision was applied keeps its
`published_at`, so after the downgrade it reads as successfully published
and its reason for failing is gone. The rows do not come back to life and
flood Kafka — which is the safe direction — but capture
`SELECT id, topic, error_message FROM outbox_events WHERE failed_at IS
NOT NULL` before downgrading if you intend to replay them by hand.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5c93b7a2d18"
down_revision: str | Sequence[str] | None = "d7b6c48f1a95"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "outbox_events",
        sa.Column("error_message", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("outbox_events", "error_message")
    op.drop_column("outbox_events", "failed_at")
