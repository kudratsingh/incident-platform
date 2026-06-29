"""platform admin flag on users

Revision ID: d9c01a7e4f30
Revises: c4f8e9a52340
Create Date: 2026-06-29 14:00:00.000000

Adds `users.is_platform_admin`. A platform admin can list, create, and
drill into any tenant; ordinary admins are still scoped to their own
tenant via RLS.

The data migration sets is_platform_admin=true for every existing
`role=admin` user in the default tenant. Rationale: before this PR
"admin" implicitly meant "platform-wide"; preserving that for the
bootstrap admin avoids locking real operators out after deploy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d9c01a7e4f30"
down_revision: str | Sequence[str] | None = "c4f8e9a52340"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirror of app.models.tenant.DEFAULT_TENANT_ID — kept as a literal so the
# migration is independent of the Python model definition.
_DEFAULT_TENANT_ID = "d3fa17de-7a17-de7a-17de-7a17de7a17de"


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_platform_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Backfill: every current role=admin user in the default tenant becomes
    # a platform admin. Without this, a freshly upgraded deployment would
    # have zero platform admins and no way to create tenants.
    op.execute(
        sa.text(
            """
            UPDATE users
               SET is_platform_admin = true
             WHERE role = 'admin'
               AND tenant_id = :tid
            """
        ).bindparams(tid=_DEFAULT_TENANT_ID)
    )


def downgrade() -> None:
    op.drop_column("users", "is_platform_admin")
