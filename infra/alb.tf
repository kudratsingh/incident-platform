resource "aws_lb" "main" {
  name               = var.app_name
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id
}

# ── Target Groups ─────────────────────────────────────────────────────────────

resource "aws_lb_target_group" "backend" {
  name        = "${var.app_name}-backend"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  # Shallow liveness, deliberately NOT the deep dependency check
  # (WO-R2-65). A target group decides who receives traffic, so the only
  # thing it may ask is whether this task can serve HTTP. `/api/v1/health`
  # returns 503 when Redis is unreachable — a dependency every path in the
  # application already fails open on — so probing it here deregistered
  # every backend target simultaneously and turned a degraded API into an
  # unreachable one. There is nothing to route around when all targets
  # share the same outage.
  #
  # Worker liveness has not been dropped; it moved to the probe that can
  # act on it, the ECS container check in ecs.tf (ADR 0009, amended).
  health_check {
    path                = "/healthz"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 30
    timeout             = 5
  }
}

resource "aws_lb_target_group" "frontend" {
  name        = "${var.app_name}-frontend"
  port        = 80
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    path              = "/"
    healthy_threshold = 2
    interval          = 30
  }
}

# ── Listener + Rules ──────────────────────────────────────────────────────────

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  # Default: serve the frontend
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.frontend.arn
  }
}

# Route /api/* to the backend
resource "aws_lb_listener_rule" "api" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 10

  condition {
    path_pattern {
      values = ["/api/*"]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.backend.arn
  }
}
