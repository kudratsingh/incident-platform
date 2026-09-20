"""agent_runs — the reasoning beside the state: hypotheses, plan, verifications, steps, budget

Revision ID: b6c1d90f4a27
Revises: a2f7c05b8e13
Create Date: 2026-09-20 10:40:00.000000

WO-R3-328 / ADR 0037. a2f7c05b8e13 stored *where* a responder was. The first live
take of the demo showed that a console cannot narrate a run from that alone: the
state said `investigating` and nothing on the screen said what was being
investigated, which hypothesis was leading, what the responder decided to do, or
whether the verify poll agreed. All of it existed inside the responder's process
and reached nobody.

Seven columns, in two shapes, and the shape is the decision:

- **Latest reading** — `hypotheses` (the ranked list, best first), `plan`,
  `verification`, `budget`. Replaced whole when a report carries them. Unlike
  `current_hypothesis`, a report that OMITS one leaves it alone: the reporter now
  sends a step-only report after every tool call, and clearing on omission would
  blank the panel between transitions.
- **Append-only** — `verifications` (every verdict, oldest first) and `steps` (the
  action ledger, one entry per call), plus `steps_dropped`.

`steps_dropped` is an integer and not a flag because the cap discards the OLDEST
entries: a console rendering 200 rows without it would describe a shorter run than
happened. NOT NULL with a server default of 0 so a row that predates this migration
counts nothing dropped rather than reading as unknown.

The three list columns are NOT NULL with a server default of `[]`, like
`phase_history`: a row always has a list to append to and no reader has to treat
NULL as empty. The caps themselves live in `app/services/agent_run.py` and are
enforced there, not here — a CHECK constraint on a JSONB array length would make a
report fail rather than shed its oldest entry, which is the wrong trade for a
fail-open telemetry write.

`phase_history` is untouched, in shape and in meaning.

No index. Every query that reads these columns has already selected the row by
primary key or by the partial active index a2f7c05b8e13 created; nothing filters on
a JSONB member.

RLS
===
Nothing to do: the policy is on the table, not on its columns, and
a2f7c05b8e13 declared `agent_runs` to the coverage gate. `_TABLES` is deliberately
absent from this module — re-declaring a table that already carries the policy would
make the gate's union say a second migration owns it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "b6c1d90f4a27"
down_revision: str | Sequence[str] | None = "a2f7c05b8e13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The JSONB list columns, with their server default. One tuple so `upgrade` and
#: `downgrade` cannot disagree about the set.
_LIST_COLUMNS = ("hypotheses", "verifications", "steps")

#: The nullable JSONB object columns.
_OBJECT_COLUMNS = ("plan", "verification", "budget")


def upgrade() -> None:
    empty_list = (
        sa.text("'[]'::jsonb")
        if op.get_bind().dialect.name == "postgresql"
        else sa.text("'[]'")
    )
    for name in _LIST_COLUMNS:
        op.add_column(
            "agent_runs",
            sa.Column(name, JSONB(), nullable=False, server_default=empty_list),
        )
    for name in _OBJECT_COLUMNS:
        op.add_column("agent_runs", sa.Column(name, JSONB(), nullable=True))
    op.add_column(
        "agent_runs",
        sa.Column("steps_dropped", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "steps_dropped")
    for name in reversed(_OBJECT_COLUMNS):
        op.drop_column("agent_runs", name)
    for name in reversed(_LIST_COLUMNS):
        op.drop_column("agent_runs", name)
