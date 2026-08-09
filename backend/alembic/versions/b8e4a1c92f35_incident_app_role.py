"""Create the non-owner incident_app runtime role (F1-01 phase 2, F1-07)

Revision ID: b8e4a1c92f35
Revises: a7e3d9c41f28
Create Date: 2026-08-09 12:00:00.000000

The role split ADR 0015 tracked as "phase 2": a7e3d9c41f28 (FORCE RLS)
made the policies bind the owner connection; this revision removes owner
power from the network-facing process entirely. `incident_app` gets DML
and nothing else — no DDL, no TRUNCATE, no DROP POLICY — and audit
tampering becomes a loud insufficient_privilege error instead of the
restrictive policies' silent no-op. The API, worker loops and MCP all
share one engine (backend/app/dependencies.py), so repointing
DATABASE_URL moves all three; migrations stay on the owner via
ALEMBIC_DATABASE_URL (backend/alembic/env.py).

Design notes, in grant order:

- CREATE ROLE is guarded (pg_roles lookup): roles are cluster-wide, not
  database- or schema-scoped, so a re-created database — or a DBA who
  pre-created the role — must not fail the deploy. LOGIN **with no
  password** is deliberate: RDS and the containers authenticate with
  scram-sha-256, so a passwordless role cannot log in until the
  boot-time sync (app/core/db_bootstrap.py) sets one. The password
  cannot live here: migrations run exactly once, on a phase-1 deploy
  that predates the Terraform secret (INCIDENT_APP_DB_PASSWORD), and the
  role would be passwordless forever. The boot-time sync is the only
  ordering-safe place — and gives free rotation.

- GRANT ... ON ALL TABLES deliberately includes alembic_version: both
  lifespans (app/main.py, app/mcp/standalone.py) run
  assert_migrations_current as incident_app at boot.

- REVOKE UPDATE, DELETE ON audit_logs: DB-enforced append-only for the
  runtime role (F1-07). The FK referential actions (ON DELETE SET NULL
  from jobs/users) keep working — they execute with the referencing
  table owner's privileges and bypass row security.

- ALTER DEFAULT PRIVILEGES covers tables created later WITHOUT a
  re-grant — but only objects created by the role that issues it, i.e.
  the master role running this migration. If migrations ever run as a
  different role, future tables silently lose incident_app grants (ADR
  0015 records this). Future *immutable* tables need their own explicit
  REVOKE — the default privileges hand out full DML.

- No sequence grants: every PK is a client-generated UUID (verified
  against Base.metadata; the only integer column, job_events.kafka_offset,
  is a plain BigInteger, not a sequence).

downgrade() reverses the default-privileges grant, then DROP OWNED BY
revokes every remaining privilege (the role owns no objects — it cannot
create any) so DROP ROLE succeeds.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b8e4a1c92f35"
down_revision: str | Sequence[str] | None = "a7e3d9c41f28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Skip on non-Postgres dialects (SQLite test DB has no roles).
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'incident_app') THEN
                CREATE ROLE incident_app LOGIN;
            END IF;
        END
        $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO incident_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO incident_app"
    )
    op.execute("REVOKE UPDATE, DELETE ON audit_logs FROM incident_app")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public"
        " GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO incident_app"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Guarded like the upgrade: everything references the role, so a
    # missing role means there is simply nothing to reverse.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'incident_app') THEN
                ALTER DEFAULT PRIVILEGES IN SCHEMA public
                    REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM incident_app;
                -- Revokes all remaining privileges; the role owns no
                -- objects (it has no CREATE anywhere), so nothing drops.
                DROP OWNED BY incident_app;
                DROP ROLE incident_app;
            END IF;
        END
        $$
        """
    )
