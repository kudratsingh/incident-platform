# ── Cluster ───────────────────────────────────────────────────────────────────

resource "aws_ecs_cluster" "main" {
  name = var.app_name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

# ── CloudWatch Log Groups ─────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "backend" {
  name              = "/ecs/${var.app_name}/backend"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "frontend" {
  name              = "/ecs/${var.app_name}/frontend"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "mcp" {
  name              = "/ecs/${var.app_name}/mcp"
  retention_in_days = 30
}

# ── Backend Task Definition ───────────────────────────────────────────────────

resource "aws_ecs_task_definition" "backend" {
  family                   = "${var.app_name}-backend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024

  execution_role_arn = aws_iam_role.ecs_execution.arn
  task_role_arn      = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "backend"
      image     = "${aws_ecr_repository.backend.repository_url}:${var.backend_image_tag}"
      essential = true

      portMappings = [
        { containerPort = 8000, protocol = "tcp" }
      ]

      # ADR 0018: the optional tails are appended via concat() so that an unset
      # variable OMITS its environment entry entirely. Passing an empty
      # KAFKA_BOOTSTRAP_SERVERS would override the app's localhost:9092 default
      # with a differently-broken value — a new failure mode, not a fix. No
      # broker is provisioned by this stack; while kafka_bootstrap_servers is
      # empty, deployed workers accept jobs and never execute them, which is
      # why the ECS deploy job is gated off (vars.ENABLE_ECS_DEPLOY).
      environment = concat(
        [
          { name = "ENVIRONMENT", value = var.environment },
          # ADR 0008 gate 1 — paired with ENVIRONMENT above: the variables.tf
          # validation refuses true+production, and app-side assert_chaos_gate()
          # re-checks the same pair at boot.
          { name = "CHAOS_ENABLED", value = var.chaos_enabled ? "true" : "false" },
          { name = "DEBUG", value = "false" },
          { name = "STORAGE_BUCKET", value = aws_s3_bucket.storage.bucket },
          { name = "AWS_DEFAULT_REGION", value = var.aws_region },
          # ALB DNS name so the backend allows cross-origin requests from the frontend.
          { name = "CORS_ORIGINS", value = "[\"http://${aws_lb.main.dns_name}\"]" },
          { name = "ALERT_WEBHOOK_URL", value = var.alert_webhook_url },
        ],
        var.kafka_bootstrap_servers == "" ? [] : [
          { name = "KAFKA_BOOTSTRAP_SERVERS", value = var.kafka_bootstrap_servers },
        ],
        var.otlp_endpoint == "" ? [] : [
          { name = "OTLP_ENDPOINT", value = var.otlp_endpoint },
        ],
      )

      # NOTE (WO-P2-03): the backend service has lifecycle
      # ignore_changes = [task_definition] — CI's deploy job re-renders
      # only the image on the current family revision, so the revision
      # registered here (with the two new secrets) is picked up by the
      # NEXT CI deploy. Apply Terraform BEFORE triggering that deploy.
      secrets = [
        {
          name      = "DATABASE_URL"
          valueFrom = aws_secretsmanager_secret.database_url.arn
        },
        {
          # Owner (RDS master) URL for `alembic upgrade head` and the
          # db_bootstrap password sync in scripts/entrypoint.sh; the app
          # itself connects with DATABASE_URL above (incident_app).
          name      = "ALEMBIC_DATABASE_URL"
          valueFrom = aws_secretsmanager_secret.database_url_owner.arn
        },
        {
          name      = "INCIDENT_APP_DB_PASSWORD"
          valueFrom = aws_secretsmanager_secret.app_db_password.arn
        },
        {
          name      = "SECRET_KEY"
          valueFrom = aws_secretsmanager_secret.secret_key.arn
        },
        {
          name      = "ALERT_WEBHOOK_SECRET"
          valueFrom = aws_secretsmanager_secret.alert_webhook_secret.arn
        },
        {
          # rediss:// URL embedding the ElastiCache AUTH token — a credential,
          # so it must not appear in the task definition's plaintext environment.
          name      = "REDIS_URL"
          valueFrom = aws_secretsmanager_secret.redis_url.arn
        },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.backend.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "backend"
        }
      }

      # Task liveness including the in-process worker, and nothing else
      # (WO-R2-65). This is the probe with restart authority, so it must
      # fire for conditions a replacement task actually fixes: a worker
      # that died and could not be restarted in-process (ADR 0009), not a
      # shared dependency being down. Curling `/api/v1/health` here meant a
      # Redis outage recycled every task mid-job, destroying in-flight work
      # to arrive back at the same outage.
      healthCheck = {
        command     = ["CMD-SHELL", "curl -f http://localhost:8000/healthz/worker || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }
    }
  ])
}

# ── Frontend Task Definition ──────────────────────────────────────────────────

resource "aws_ecs_task_definition" "frontend" {
  family                   = "${var.app_name}-frontend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512

  execution_role_arn = aws_iam_role.ecs_execution.arn

  container_definitions = jsonencode([
    {
      name      = "frontend"
      image     = "${aws_ecr_repository.frontend.repository_url}:${var.frontend_image_tag}"
      essential = true

      portMappings = [
        { containerPort = 80, protocol = "tcp" }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.frontend.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "frontend"
        }
      }
    }
  ])
}

# ── MCP Task Definition ───────────────────────────────────────────────────────
#
# ADR 0006 chose a standalone process for the MCP surface and accepted "a
# second deployable: compose stanza, ECS service, health check, alarms" as
# the cost. The compose stanza shipped; this is the rest of it (WO-R2-68).
# Until now the only description of production this repo has said the agent
# surface did not exist there, while ADR 0006 and ARCHITECTURE.md both said
# it did.
#
# Same image as the backend, different command — that is the whole of the
# "no second build" claim in the ADR, and it is why this uses
# `var.backend_image_tag` rather than a tag variable of its own. Two tags
# could skew, and a skew would mean the MCP surface fronting a different
# commit's service layer than the REST surface, which is the exact drift
# the ADR chose this topology to prevent.
resource "aws_ecs_task_definition" "mcp" {
  family                   = "${var.app_name}-mcp"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  # Smaller than the backend on purpose: this process serves tool calls
  # only. It runs no worker, no consumers, no background loops — those all
  # live in the API process — and ADR 0006 pairs it with "a small pool
  # matched to its rate limits".
  cpu    = 256
  memory = 512

  execution_role_arn = aws_iam_role.ecs_execution.arn
  task_role_arn      = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "mcp"
      image     = "${aws_ecr_repository.backend.repository_url}:${var.backend_image_tag}"
      essential = true

      # The compose stanza's command, minus --reload. `scripts/entrypoint.sh`
      # is deliberately bypassed: it runs `alembic upgrade head`, and this
      # process must not migrate. The backend task already does, exactly once
      # per deploy, under the advisory lock in app/core/migration_lock.py.
      command = [
        "uvicorn", "app.mcp.standalone:app",
        "--host", "0.0.0.0",
        "--port", "8001",
      ]

      portMappings = [
        { containerPort = 8001, protocol = "tcp" }
      ]

      environment = concat(
        [
          { name = "ENVIRONMENT", value = var.environment },
          # ADR 0008 gate 1. Evaluated at *import* time by the @chaos_tool
          # decorators, so this decides whether the five chaos tools appear
          # in this process's tools/list at all. variables.tf refuses
          # true+production and the app re-checks the pair at boot.
          { name = "CHAOS_ENABLED", value = var.chaos_enabled ? "true" : "false" },
          { name = "DEBUG", value = "false" },
          { name = "AWS_DEFAULT_REGION", value = var.aws_region },
          { name = "PYTHONPATH", value = "/app/backend" },
          { name = "ALERT_WEBHOOK_URL", value = var.alert_webhook_url },
        ],
        # Same ADR 0018 treatment as the backend: an unset variable omits the
        # entry rather than overriding the app default with an empty string.
        # `poison_message` is the one tool that opens a direct Kafka
        # connection; without a broker it is the only tool that fails, and it
        # fails the same way it does for the backend.
        var.kafka_bootstrap_servers == "" ? [] : [
          { name = "KAFKA_BOOTSTRAP_SERVERS", value = var.kafka_bootstrap_servers },
        ],
        var.otlp_endpoint == "" ? [] : [
          { name = "OTLP_ENDPOINT", value = var.otlp_endpoint },
        ],
      )

      # Deliberately NOT ALEMBIC_DATABASE_URL or INCIDENT_APP_DB_PASSWORD.
      # Those are the owner (RDS master) credential and the role-sync
      # password, needed only by the task that migrates — which this one
      # never does. A process that cannot run DDL should not be holding the
      # credential that could.
      secrets = [
        {
          name      = "DATABASE_URL"
          valueFrom = aws_secretsmanager_secret.database_url.arn
        },
        {
          # Every MCP request authenticates a machine principal through the
          # same dependency chain as REST (ADR 0006), which means verifying
          # a token signed with this key.
          name      = "SECRET_KEY"
          valueFrom = aws_secretsmanager_secret.secret_key.arn
        },
        {
          name      = "REDIS_URL"
          valueFrom = aws_secretsmanager_secret.redis_url.arn
        },
        {
          # Only reachable here through the `bad_deploy` chaos tool, which
          # cannot register in production (ADR 0008). Present so that a
          # chaos-enabled non-production stack signs its webhook the same way
          # the backend does, rather than silently not delivering.
          name      = "ALERT_WEBHOOK_SECRET"
          valueFrom = aws_secretsmanager_secret.alert_webhook_secret.arn
        },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.mcp.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "mcp"
        }
      }

      healthCheck = {
        command     = ["CMD-SHELL", "curl -f http://localhost:8001/healthz || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }
    }
  ])
}

# ── Services ──────────────────────────────────────────────────────────────────

resource "aws_ecs_service" "backend" {
  name            = "${var.app_name}-backend"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.backend.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.backend.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.backend.arn
    container_name   = "backend"
    container_port   = 8000
  }

  depends_on = [aws_lb_listener_rule.api]

  # Ignore image tag changes — CI updates these via task definition revisions, not Terraform.
  lifecycle {
    ignore_changes = [task_definition]
  }
}

resource "aws_ecs_service" "mcp" {
  name            = "${var.app_name}-mcp"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.mcp.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.mcp.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.mcp.arn
    container_name   = "mcp"
    container_port   = 8001
  }

  depends_on = [aws_lb_listener_rule.mcp]

  # Same reasoning as the backend service: CI rolls images by registering a
  # new task-definition revision, so Terraform must not fight it.
  lifecycle {
    ignore_changes = [task_definition]
  }
}

resource "aws_ecs_service" "frontend" {
  name            = "${var.app_name}-frontend"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.frontend.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.frontend.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.frontend.arn
    container_name   = "frontend"
    container_port   = 80
  }

  depends_on = [aws_lb_listener.http]

  lifecycle {
    ignore_changes = [task_definition]
  }
}
