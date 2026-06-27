"""dag dependencies and sagas

Revision ID: d4b1a8e60305
Revises: c3e9f1a4d802
Create Date: 2026-06-26 00:02:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4b1a8e60305"
down_revision: str | Sequence[str] | None = "c3e9f1a4d802"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sagas",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="running"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sagas_status"), "sagas", ["status"], unique=False)

    op.add_column(
        "jobs", sa.Column("saga_id", sa.UUID(), nullable=True)
    )
    op.create_foreign_key(
        "fk_jobs_saga_id_sagas",
        "jobs",
        "sagas",
        ["saga_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_jobs_saga_id"), "jobs", ["saga_id"], unique=False)

    op.create_table(
        "job_dependencies",
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("depends_on_job_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["depends_on_job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("job_id", "depends_on_job_id"),
    )
    op.create_index(
        op.f("ix_job_dependencies_depends_on_job_id"),
        "job_dependencies",
        ["depends_on_job_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_job_dependencies_depends_on_job_id"), table_name="job_dependencies"
    )
    op.drop_table("job_dependencies")
    op.drop_index(op.f("ix_jobs_saga_id"), table_name="jobs")
    op.drop_constraint("fk_jobs_saga_id_sagas", "jobs", type_="foreignkey")
    op.drop_column("jobs", "saga_id")
    op.drop_index(op.f("ix_sagas_status"), table_name="sagas")
    op.drop_table("sagas")
