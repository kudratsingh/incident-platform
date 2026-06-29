"""tenant rate limits + monthly quotas

Revision ID: b3d8e7a52116
Revises: a9c2d1e83104
Create Date: 2026-06-29 12:00:00.000000

Adds two integer columns on tenants:

  rate_limit_per_minute  POST /jobs requests/min allowed for the tenant
  quota_jobs_per_month   total jobs that can be created in a calendar month

0 disables the check. The migration sets reasonable defaults for the
bootstrap tenant and any pre-existing tenants.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3d8e7a52116"
down_revision: str | Sequence[str] | None = "a9c2d1e83104"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "rate_limit_per_minute",
            sa.Integer(),
            nullable=False,
            server_default="120",
        ),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "quota_jobs_per_month",
            sa.Integer(),
            nullable=False,
            server_default="100000",
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "quota_jobs_per_month")
    op.drop_column("tenants", "rate_limit_per_minute")
