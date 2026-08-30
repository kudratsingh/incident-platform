"""alerts.dedup_key + uq_alerts_tenant_dedup_key (WO-R2-29)

Revision ID: c2e84a1f9d36
Revises: c9e41a7b62d5
Create Date: 2026-08-30 16:40:00.000000

Scheduled SLO evaluation gives the alerts table its first repeating producer.
Everything that wrote an alert before this was a one-shot: a human running the
`bad_deploy` chaos tool means it every time they run it. A loop that evaluates
the same objectives every 5 minutes does not — a burn lasting an hour is one
condition observed twelve times, and without de-duplication it would be twelve
alert rows and twelve webhook deliveries.

`dedup_key` is the producer's name for the condition-and-window it is
reporting, and the unique constraint is what makes de-duplication *safe*
rather than merely likely. `worker_loop` runs in every API replica, so a
"look for a recent alert, then insert" check is a check-then-act race that
two replicas both win. With uniqueness the database settles it: one insert
lands, the other raises IntegrityError and its producer knows the alert
already exists.

The constraint is on `(tenant_id, dedup_key)`, not `dedup_key` alone, because
alerts are tenant-scoped everywhere else in the system and two tenants must be
able to report the same condition independently.

Nullable, and NULL is the normal case: only producers that repeat need a key.
Multiple NULLs coexist under a UNIQUE constraint on both Postgres and SQLite
(SQL's "NULLs are distinct" rule), so every existing row and every one-shot
producer is unaffected — no backfill, and nothing to exclude with a partial
index.

String(128) is sized for the longest key the SLO producer builds
(`slo:job_dispatch_latency:fast_burn:<bucket>`, ~45 chars) with room for
producers that namespace more deeply. It is deliberately not TEXT: an
unbounded key in a unique index is an unbounded index entry.

No separate index is created — the unique constraint provides one, and
`(tenant_id, dedup_key)` is the only shape anything looks the column up by.

No grant needed for the incident_app runtime role (b8e4a1c92f35): a
table-level GRANT in Postgres covers columns added later.

Downgrade drops the constraint and the column. Lossy only in that repeating
producers lose their de-duplication and will mint an alert per evaluation
tick until code matching this schema is deployed again; no alert history is
destroyed, since the column is metadata about *why* a row exists rather than
part of the alert itself.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2e84a1f9d36"
down_revision: str | Sequence[str] | None = "c9e41a7b62d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "alerts",
        sa.Column("dedup_key", sa.String(length=128), nullable=True),
    )
    op.create_unique_constraint(
        "uq_alerts_tenant_dedup_key", "alerts", ["tenant_id", "dedup_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_alerts_tenant_dedup_key", "alerts", type_="unique")
    op.drop_column("alerts", "dedup_key")
