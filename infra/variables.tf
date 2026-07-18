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
# workspace. The validation below is the load-bearing check; even if
# someone manually sets the variable, `terraform apply` refuses to
# proceed. The app-side `assert_chaos_gate()` catches the case where
# an operator somehow bypasses Terraform entirely.
# ------------------------------------------------------------------
variable "chaos_enabled" {
  description = "Enable the chaos framework in this workspace. Must be false when environment = production."
  type        = bool
  default     = false

  validation {
    condition     = !(var.chaos_enabled)
    error_message = "Chaos is disabled by default in every workspace. To enable in a non-production workspace, comment out this validation block and set chaos_enabled=true. Never enable in production — the app also refuses to boot with CHAOS_ENABLED=true when ENVIRONMENT=production."
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
