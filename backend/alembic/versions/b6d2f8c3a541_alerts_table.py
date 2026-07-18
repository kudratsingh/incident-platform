"""alerts table

Revision ID: b6d2f8c3a541
Revises: a5c19d3f7e42
Create Date: 2026-07-17 20:00:00.000000

Wave 1 PR B. Persistent record of every alert the platform emits.
The `list_active_alerts` MCP tool reads unresolved rows (`resolved_at
IS NULL`); the outbound webhook fires from the service layer on
create when a webhook URL + secret are configured.

Composite partial index on `(tenant_id, resolved_at, severity)` where
resolved_at IS NULL — that's the shape `list_active_alerts` scans
every time an agent polls. Tenant scoping + severity secondary sort
lands with one index probe.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "b6d2f8c3a541"
down_revision: str | Sequence[str] | None = "a5c19d3f7e42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alerts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.String(2048), nullable=True),
        sa.Column(
            "fired_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("extra_data", JSONB(), nullable=True),
        sa.Column("request_id", sa.String(255), nullable=True),
    )
    op.create_index("ix_alerts_tenant_id", "alerts", ["tenant_id"])
    op.create_index("ix_alerts_severity", "alerts", ["severity"])
    op.create_index("ix_alerts_source", "alerts", ["source"])
    # Active-alerts scan path. Partial index (WHERE resolved_at IS NULL)
    # keeps it small even when the alerts table grows unboundedly.
    op.create_index(
        "ix_alerts_active",
        "alerts",
        ["tenant_id", "severity", sa.text("fired_at DESC")],
        postgresql_where=sa.text("resolved_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_alerts_active", table_name="alerts")
    op.drop_index("ix_alerts_source", table_name="alerts")
    op.drop_index("ix_alerts_severity", table_name="alerts")
    op.drop_index("ix_alerts_tenant_id", table_name="alerts")
    op.drop_table("alerts")
