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
