resource "random_password" "db" {
  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "random_password" "secret_key" {
  length  = 64
  special = false
}

# ── DATABASE_URL ──────────────────────────────────────────────────────────────

resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${var.app_name}/database-url"
  description             = "Full asyncpg connection string for the backend"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id = aws_secretsmanager_secret.database_url.id

  # Built from RDS endpoint after the instance is created.
  secret_string = "postgresql+asyncpg://${var.db_username}:${random_password.db.result}@${aws_db_instance.main.endpoint}/${var.db_name}"
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
