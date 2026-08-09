resource "random_password" "db" {
  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "random_password" "secret_key" {
  length  = 64
  special = false
}

# ── DATABASE_URL — runtime (incident_app) ─────────────────────────────────────
# Two-URL scheme (ADR 0015 / WO-P2-03): the runtime connects as the non-owner
# incident_app role; migrations + the boot-time password sync keep the RDS
# master via database-url-owner below. The role itself is created by alembic
# migration b8e4a1c92f35; its password is synced at every boot by
# `python -m app.core.db_bootstrap` from app-db-password.
#
# ROLLOUT ORDER (phase 2): apply this flip only AFTER the phase-1 image (whose
# migration creates the role) has deployed, then force a new ECS deployment.
# Applying early cannot break a RUNNING task (ECS injects secrets at task
# start), but any restart before the migration has run crash-loops on auth.

resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${var.app_name}/database-url"
  description             = "Runtime asyncpg connection string for the backend (non-owner incident_app role)"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id = aws_secretsmanager_secret.database_url.id

  # Built from RDS endpoint after the instance is created.
  secret_string = "postgresql+asyncpg://incident_app:${random_password.app_db.result}@${aws_db_instance.main.endpoint}/${var.db_name}"
}

# ── ALEMBIC_DATABASE_URL — owner (RDS master) ─────────────────────────────────

resource "aws_secretsmanager_secret" "database_url_owner" {
  name                    = "${var.app_name}/database-url-owner"
  description             = "Owner (RDS master) connection string — alembic migrations and db_bootstrap only"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "database_url_owner" {
  secret_id = aws_secretsmanager_secret.database_url_owner.id

  # The exact URL the runtime used before the incident_app flip.
  secret_string = "postgresql+asyncpg://${var.db_username}:${random_password.db.result}@${aws_db_instance.main.endpoint}/${var.db_name}"
}

# ── INCIDENT_APP_DB_PASSWORD ──────────────────────────────────────────────────

# special=false is deliberate: the password is embedded in the runtime URL
# above, and the random_password.db override_special set (#$&<>:? etc.) would
# need URL-encoding — a pre-existing latent bug on the master URL that stays
# out of scope. Do NOT touch random_password.db: changing it rotates the live
# RDS master password.
resource "random_password" "app_db" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "app_db_password" {
  name                    = "${var.app_name}/app-db-password"
  description             = "Password for the non-owner incident_app role; synced at boot by db_bootstrap"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "app_db_password" {
  secret_id     = aws_secretsmanager_secret.app_db_password.id
  secret_string = random_password.app_db.result
}

# ── SECRET_KEY ────────────────────────────────────────────────────────────────

resource "aws_secretsmanager_secret" "secret_key" {
  name                    = "${var.app_name}/secret-key"
  description             = "JWT signing secret for the backend"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "secret_key" {
  secret_id     = aws_secretsmanager_secret.secret_key.id
  secret_string = random_password.secret_key.result
}

# ── ALERT_WEBHOOK_SECRET ──────────────────────────────────────────────────────

resource "aws_secretsmanager_secret" "alert_webhook_secret" {
  name                    = "${var.app_name}/alert-webhook-secret"
  description             = "HMAC-SHA256 shared secret for signing outbound alert webhook bodies"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "alert_webhook_secret" {
  secret_id     = aws_secretsmanager_secret.alert_webhook_secret.id
  secret_string = var.alert_webhook_secret
}

# ── REDIS_URL ─────────────────────────────────────────────────────────────────

resource "aws_secretsmanager_secret" "redis_url" {
  name                    = "${var.app_name}/redis-url"
  description             = "Full rediss:// connection string for the backend (embeds the ElastiCache AUTH token)"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "redis_url" {
  secret_id = aws_secretsmanager_secret.redis_url.id

  # Built from the ElastiCache primary endpoint after the replication group is
  # created. rediss:// scheme: in-transit encryption is enabled on the group.
  secret_string = "rediss://:${random_password.redis_auth.result}@${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0"
}
