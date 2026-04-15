# ── SNS Topic ─────────────────────────────────────────────────────────────────
# All alarms publish here. Subscribe an email address via var.alarm_email.

resource "aws_sns_topic" "alarms" {
  name = "${var.app_name}-alarms"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alarm_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# ── Alarm 1: ALB 5xx error rate ───────────────────────────────────────────────
# Fires when the backend returns more than 10 5xx responses in a 1-minute window.

resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  alarm_name          = "${var.app_name}-alb-5xx-high"
  alarm_description   = "Backend is returning elevated 5xx errors — check ECS logs and DB connectivity."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "HTTPCode_Target_5XX_Count"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Sum"
  threshold           = 10
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = aws_lb.main.arn_suffix
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

# ── Alarm 2: ECS backend running task count ───────────────────────────────────
# Fires when no backend tasks are running (service is down).
# Requires Container Insights, which is enabled on the cluster in ecs.tf.

resource "aws_cloudwatch_metric_alarm" "ecs_backend_tasks" {
  alarm_name          = "${var.app_name}-backend-tasks-low"
  alarm_description   = "No backend ECS tasks are running — the service may be crash-looping."
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  metric_name         = "RunningTaskCount"
  namespace           = "ECS/ContainerInsights"
  period              = 60
  statistic           = "Average"
  threshold           = 1
  treat_missing_data  = "breaching"

  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.backend.name
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

# ── Alarm 3: RDS CPU ──────────────────────────────────────────────────────────
# Fires when Postgres CPU exceeds 80% for 3 consecutive minutes.

resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  alarm_name          = "${var.app_name}-rds-cpu-high"
  alarm_description   = "RDS CPU above 80% — check for slow queries or missing indexes."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "CPUUtilization"
  namespace           = "AWS/RDS"
  period              = 60
  statistic           = "Average"
  threshold           = 80
  treat_missing_data  = "notBreaching"

  dimensions = {
    DBInstanceIdentifier = aws_db_instance.main.id
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

# ── Alarm 4: ElastiCache freeable memory ─────────────────────────────────────
# Fires when Redis has less than 50 MB of freeable memory.
# cache.t3.micro has ~512 MB total; 50 MB headroom is a reasonable warning threshold.

resource "aws_cloudwatch_metric_alarm" "redis_memory" {
  alarm_name          = "${var.app_name}-redis-memory-low"
  alarm_description   = "Redis freeable memory below 50 MB — evictions may begin soon."
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  metric_name         = "FreeableMemory"
  namespace           = "AWS/ElastiCache"
  period              = 60
  statistic           = "Average"
  threshold           = 52428800 # 50 MB in bytes

  dimensions = {
    CacheClusterId = aws_elasticache_cluster.redis.id
    CacheNodeId    = "0001"
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

# ── Alarm 5: Job queue depth (custom metric) ──────────────────────────────────
# The dispatcher emits QueueDepth to the IncidentPlatform namespace every ~60 s.
# Fires when more than 50 jobs are waiting — worker may be overwhelmed.

resource "aws_cloudwatch_metric_alarm" "queue_depth" {
  alarm_name          = "${var.app_name}-queue-depth-high"
  alarm_description   = "Job queue depth above 50 — worker throughput may be insufficient."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "QueueDepth"
  namespace           = "IncidentPlatform"
  period              = 60
  statistic           = "Maximum"
  threshold           = 50
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}
