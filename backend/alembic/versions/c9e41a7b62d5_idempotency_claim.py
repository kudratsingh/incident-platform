"""idempotency_records.response_json nullable — claim before execute

Revision ID: c9e41a7b62d5
Revises: b1f39d7c2a84
Create Date: 2026-08-30 14:20:00.000000

Why: the MCP handler now reserves an idempotency key *before* running the
action instead of recording it afterwards (WO-R2-27). The reservation row
exists before there is any response to put in it, so `response_json` has
to accept NULL for the window between the claim and its completion.

NULL is a state, not a missing value: it means "claimed, not yet
answered". The claim and its response commit in the same transaction, so
another caller normally never observes one — the column is nullable to
make the intermediate state representable, not because responses became
optional.

Why this ordering matters at all: the previous shape looked the key up
and inserted it after execution, both inside one READ COMMITTED
transaction with nothing in between. Two concurrent calls on the same key
both missed the cache, both executed the action, and the loser then
violated `uq_idempotency_scope` on the way out — a duplicate-key error
raised after its side effect had already landed. An expired-but-unreaped
record produced the same collision on its own, because the lookup treated
it as absent while the unique index went on holding it.

Backfill: none needed. Every existing row was written by the old
store-after-execute path and already carries a response.

Downgrade drops the rows that only the new shape can produce — records
with a NULL response, i.e. claims that were never completed — and then
restores NOT NULL. Those rows are unfinished claims, never answers, so
deleting them loses no recorded outcome: the effect is that any key held
by an in-flight call at downgrade time becomes free again, which is the
same state a release would have left. Doing it the other way (defaulting
them to `{}`) would fabricate an empty response and make a retry replay
nothing, which is worse than re-executing.

SQLite cannot ALTER a column's nullability in place, so both directions
go through Alembic's batch mode, which rebuilds the table. Postgres uses
the plain ALTER path.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9e41a7b62d5"
down_revision: str | Sequence[str] | None = "b1f39d7c2a84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("idempotency_records") as batch:
        batch.alter_column(
            "response_json",
            existing_type=sa.JSON(),
            nullable=True,
        )


def downgrade() -> None:
    # Unfinished claims cannot satisfy NOT NULL and carry no answer worth
    # keeping. Drop them, then restore the constraint.
    op.execute(
        sa.text(
            "DELETE FROM idempotency_records WHERE response_json IS NULL"
        )
    )
    with op.batch_alter_table("idempotency_records") as batch:
        batch.alter_column(
            "response_json",
            existing_type=sa.JSON(),
            nullable=False,
        )
