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
  alarm_description   = "Backend is returning elevated 5xx errors. Runbook: rb-alb-5xx (/admin/runbooks/rb-alb-5xx). Check ECS logs and DB connectivity."
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
  alarm_description   = "No backend ECS tasks are running. Runbook: rb-ecs-tasks-low (/admin/runbooks/rb-ecs-tasks-low). The service may be crash-looping."
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
  alarm_description   = "RDS CPU above 80%. Runbook: rb-rds-cpu-high (/admin/runbooks/rb-rds-cpu-high). Check for slow queries or missing indexes."
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
  alarm_description   = "Redis freeable memory below 50 MB. Runbook: rb-redis-memory-low (/admin/runbooks/rb-redis-memory-low). Evictions may begin soon."
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  metric_name         = "FreeableMemory"
  namespace           = "AWS/ElastiCache"
  period              = 60
  statistic           = "Average"
  threshold           = 52428800 # 50 MB in bytes

  dimensions = {
    # The replication group has exactly one member cluster (num_cache_clusters
    # = 1); one() fails the plan if that changes, forcing this alarm to be
    # revisited alongside any scale-out.
    CacheClusterId = one(aws_elasticache_replication_group.redis.member_clusters)
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
  alarm_description   = "Job queue depth above 50. Runbook: rb-queue-depth-high (/admin/runbooks/rb-queue-depth-high). Worker throughput may be insufficient."
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

# ── Alarm 6: Job-completion SLO fast-burn ─────────────────────────────────────
# Fires when the failure rate over the last hour would, if sustained, exhaust
# the 30-day error budget in under 2 hours. The math: at a 1.0× burn rate the
# whole budget lasts the full window; the canonical fast-burn threshold is
# 14.4× over a 1h sliding window. SLO target is 99% (budget = 1%), so
# fast-burn failure rate threshold = 14.4 * 0.01 = 0.144 — i.e. 14.4% of jobs
# in the last hour dead-lettering.
#
# Implemented as a math expression on the two custom metrics. CloudWatch
# doesn't have a ratio aggregation natively, so we compute it here.

resource "aws_cloudwatch_metric_alarm" "slo_job_completion_fast_burn" {
  alarm_name          = "${var.app_name}-slo-job-completion-fast-burn"
  alarm_description   = "Job-completion SLO burning at 14.4× normal — will exhaust 30-day error budget in <2h. Runbook: rb-slo-job-completion (/admin/runbooks/rb-slo-job-completion)."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0.144
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "failure_rate"
    expression  = "IF((dl + ok) > 0, dl / (dl + ok), 0)"
    label       = "Job dead-letter rate"
    return_data = true
  }

  metric_query {
    id = "dl"
    metric {
      metric_name = "JobDeadLettered"
      namespace   = "IncidentPlatform"
      period      = 3600
      stat        = "Sum"
    }
  }

  metric_query {
    id = "ok"
    metric {
      metric_name = "JobCompleted"
      namespace   = "IncidentPlatform"
      period      = 3600
      stat        = "Sum"
    }
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

# ── Alarm 7: Job-dispatch latency SLO fast-burn ──────────────────────────────
# Approximation: if QueueDepth stays > 100 for 15 min, we're not meeting the
# 30-second dispatch SLO at any reasonable arrival rate. A precise version
# would compute per-job started_at - created_at via CloudWatch Metric Math,
# but QueueDepth is a good cheap proxy.

resource "aws_cloudwatch_metric_alarm" "slo_dispatch_latency_fast_burn" {
  alarm_name          = "${var.app_name}-slo-dispatch-latency-fast-burn"
  alarm_description   = "Job-dispatch latency SLO burning fast — queue is backed up. Runbook: rb-slo-dispatch-latency (/admin/runbooks/rb-slo-dispatch-latency)."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "QueueDepth"
  namespace           = "IncidentPlatform"
  period              = 300
  statistic           = "Average"
  threshold           = 100
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}
