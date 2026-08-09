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

      healthCheck = {
        command     = ["CMD-SHELL", "curl -f http://localhost:8000/api/v1/health || exit 1"]
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
