variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "app_name" {
  description = "Application name — used as a prefix for all resources"
  type        = string
  default     = "incident-platform"
}

variable "environment" {
  description = "Deployment environment"
  type        = string
  default     = "production"
}

variable "db_name" {
  description = "PostgreSQL database name"
  type        = string
  default     = "incident_platform"
}

variable "db_username" {
  description = "PostgreSQL master username"
  type        = string
  default     = "appuser"
}

# Passed by CI on each deploy — defaults to latest for local terraform apply.
variable "backend_image_tag" {
  description = "Docker image tag for the backend service"
  type        = string
  default     = "latest"
}

variable "frontend_image_tag" {
  description = "Docker image tag for the frontend service"
  type        = string
  default     = "latest"
}

variable "alarm_email" {
  description = "Email address for CloudWatch alarm notifications (leave empty to skip subscription)"
  type        = string
  default     = ""
}

# ------------------------------------------------------------------
# Chaos framework — see ADR 0008.
#
# `chaos_enabled=true` MUST NOT be permitted in the production
# workspace. The validation below is the load-bearing check: plan and
# apply refuse the chaos_enabled=true + environment="production"
# combination, while any non-production workspace may enable chaos
# freely. The value is wired into the backend task definition as
# CHAOS_ENABLED (ecs.tf); the app-side `assert_chaos_gate()` re-checks
# the same pair at boot and catches the case where an operator somehow
# bypasses Terraform entirely. The `infra` CI job proves the refusal
# with a credential-free negative plan on every push.
# ------------------------------------------------------------------
variable "chaos_enabled" {
  description = "Enable the chaos framework in this workspace. Must be false when environment = production."
  type        = bool
  default     = false

  validation {
    condition     = !(var.chaos_enabled && var.environment == "production")
    error_message = "chaos_enabled=true is refused in the production workspace (ADR 0008): the chaos framework must never be deployable to production. Chaos may be enabled in any other workspace by setting environment to a value other than \"production\". The app enforces the same invariant at boot — assert_chaos_gate() refuses CHAOS_ENABLED=true when ENVIRONMENT=production."
  }
}

# ------------------------------------------------------------------
# Alert emission — outbound signed webhook for MCP-driven alerts.
#
# `alert_webhook_url` is optional; when unset alerts are still
# persisted and reachable via the list_active_alerts MCP tool, just
# not pushed. `alert_webhook_secret` is the HMAC-SHA256 shared secret.
# ------------------------------------------------------------------
variable "alert_webhook_url" {
  description = "Optional webhook URL for outbound alert notifications"
  type        = string
  default     = ""
}

variable "alert_webhook_secret" {
  description = "HMAC-SHA256 shared secret for signing outbound alert bodies"
  type        = string
  default     = ""
  sensitive   = true
}

# ------------------------------------------------------------------
# Kafka + tracing endpoints — see ADR 0018.
#
# There is no production broker in this stack: no MSK cluster, no
# self-managed nodes, nothing. Both variables default to "" and the
# ecs.tf task definition OMITS the corresponding environment entry when
# empty, rather than passing "". That distinction is load-bearing —
# `KAFKA_BOOTSTRAP_SERVERS=""` would override the app's own default
# (localhost:9092) with a differently-broken value, changing the
# failure mode without fixing anything. Omission leaves exactly one
# documented behaviour.
# ------------------------------------------------------------------
variable "kafka_bootstrap_servers" {
  description = "Kafka bootstrap servers for the deployed backend. REQUIRED for job execution: no broker is provisioned by this stack (ADR 0018), and while this is empty the env var is omitted, the app falls back to localhost:9092, and deployed workers accept jobs but never execute them. Set to an MSK, self-managed, or external broker before enabling the ECS deploy."
  type        = string
  default     = ""
}

variable "otlp_endpoint" {
  description = "OTLP HTTP endpoint for trace export (e.g. an X-Ray/ADOT collector). Omitted from the task definition when empty, which leaves tracing disabled — see ADR 0018."
  type        = string
  default     = ""
}
