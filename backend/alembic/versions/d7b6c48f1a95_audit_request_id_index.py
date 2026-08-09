"""audit_logs.request_id index (D-02)

Revision ID: d7b6c48f1a95
Revises: c9a3e5d70b12
Create Date: 2026-08-09 16:00:00.000000

Why: the MCP `get_trace` tool now filters audit rows by `request_id` in
SQL. It used to pull the newest 200 audit rows unfiltered and match
`request_id` in Python, which decayed as the table grew — every MCP call
appends an `agent.tool_invoked` row, so after ~200 tool calls a trace's
own rows fell out of the window and `get_trace` returned an empty
`audit_events` list for rows still present in the DB.

`audit_logs` is the hottest-growing table in the schema, so an equality
predicate on an unindexed `request_id` is a sequential scan that gets
worse every day. Plain (non-unique) B-tree: request_id is nullable and
many rows legitimately share one correlation id (a single request writes
several audit rows).

Index name matches SQLAlchemy's default for `index=True` on the mapped
column (`ix_<table>_<column>`), so the model and this migration agree
and autogenerate stays quiet.

No grant needed for the incident_app runtime role (b8e4a1c92f35): index
creation does not change table privileges.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d7b6c48f1a95"
down_revision: str | Sequence[str] | None = "c9a3e5d70b12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_audit_logs_request_id", "audit_logs", ["request_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_request_id", table_name="audit_logs")
