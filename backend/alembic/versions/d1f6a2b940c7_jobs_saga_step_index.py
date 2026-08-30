"""jobs.saga_step_index — declaration order for saga steps (WO-R2-58)

Revision ID: d1f6a2b940c7
Revises: c2e84a1f9d36
Create Date: 2026-08-30 17:10:00.000000

Saga steps had no recorded order. `SagaRepository.completed_steps` inferred
it from `created_at`, and `TimestampMixin.created_at` defaults to `func.now()`
— which Postgres resolves to `transaction_timestamp()`, one value for the
whole transaction. Every step of a saga is inserted by a single `POST /sagas`
request, so they all carry an *identical* timestamp and `ORDER BY created_at`
over them is a total tie the planner is free to break any way it likes. The
compensation rollback order derived from that tie was therefore arbitrary,
which makes "undo the most recent success first" a coin flip rather than a
guarantee.

A timestamp cannot be repaired into a sequence; the order has to be written
down at creation. Hence this column: 0-based, in declaration order, stamped by
`SagaService.create_saga`.

Nullable, and NULL is the normal case for most of the table — only declared
saga steps carry a value. Ordinary jobs have no saga to be a step of, and the
`.compensate` rows `SagaCoordinator` mints are deliberately left NULL: they are
ordered by the steps they undo, not by a position of their own, and giving them
indices would interleave them with the originals under any ORDER BY.

INTEGER rather than SMALLINT for no better reason than that `MAX_SAGA_STEPS`
(50) could grow and INTEGER is what every other counter on this table uses.
No CHECK constraint on the range: the value is assigned by one call site from
`enumerate()`, and a constraint would buy nothing a test does not already.

BACKFILL. Existing saga rows get an index from the only ordering available to
them — `(created_at, id)`. That is *arbitrary* for the steps of any one saga,
exactly as the defect describes, so the backfill does not recover a truth that
was lost; it freezes one ordering so the fallback path in the repository is
dead for all existing data and the query has a single shape. Sagas already
settled are unaffected either way. `.compensate` rows are excluded so they keep
the NULL the new writer would give them.

Adding a nullable column with no default is a catalogue-only change in
Postgres — no table rewrite. The backfill UPDATE does touch every saga row;
`jobs.saga_id` is indexed and saga rows are a small minority of the table, so
this is a bounded write rather than a full scan of `jobs`.

No new index. The only query that orders by this column also filters
`saga_id = ?`, which `ix_jobs_saga_id` already serves, and a saga is capped at
`MAX_SAGA_STEPS` rows — sorting 50 rows in memory is not worth an index to
maintain on every job insert.

No grant needed for the `incident_app` runtime role (b8e4a1c92f35): a
table-level GRANT in Postgres covers columns added later.

Downgrade drops the column. Lossy: declaration order is gone again and
compensation falls back to the arbitrary `(created_at, id)` order the
repository still uses for rows without an index.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1f6a2b940c7"
down_revision: str | Sequence[str] | None = "c2e84a1f9d36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("saga_step_index", sa.Integer(), nullable=True),
    )
    op.execute(
        sa.text(
            """
            WITH ordered AS (
                SELECT
                    id,
                    row_number() OVER (
                        PARTITION BY saga_id ORDER BY created_at, id
                    ) - 1 AS idx
                FROM jobs
                WHERE saga_id IS NOT NULL
                  AND type NOT LIKE '%.compensate'
            )
            UPDATE jobs
            SET saga_step_index = ordered.idx
            FROM ordered
            WHERE jobs.id = ordered.id
            """
        )
    )


def downgrade() -> None:
    op.drop_column("jobs", "saga_step_index")
