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
    # `.identifier`, not `.id`: under the pinned AWS provider (~> 5.0)
    # aws_db_instance.id is the DBI *resource* id ("db-ABC123…"), while the
    # CloudWatch dimension carries the instance identifier ("incident-platform").
    # Both are plausible-looking strings, so the wrong one produces a valid
    # plan and an alarm that never receives a datapoint.
    DBInstanceIdentifier = aws_db_instance.main.identifier
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
  # Absence of datapoints is not evidence of low memory, so this matches its
  # AWS-namespace siblings rather than the "breaching" the ECS task-count
  # alarm uses (there, absence *is* the outage). Stated explicitly because the
  # unset default is "missing", which pins the alarm to whatever state it last
  # held — a node that stops reporting entirely would keep reading OK.
  treat_missing_data = "notBreaching"

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

# ── Alarm 5: Job backlog (custom metric) ─────────────────────────────────────
# Fires when more than 50 jobs are waiting — worker throughput may be
# insufficient.
#
# Reads ConsumerLag, NOT QueueDepth. The dispatcher emits both every ~60 s from
# _metrics_loop, but they measure different things: QueueDepth is
# `queue.delayed_length(redis)`, the size of the Redis *delayed-retry* sorted
# set, while the primary job queue moved to Kafka in Phase 7. A genuine
# backlog — jobs produced faster than the consumer drains them — accumulates
# as consumer lag and leaves the delayed set completely untouched, so the
# QueueDepth version of this alarm read green through exactly the condition it
# was named for.
#
# Caveat worth knowing at 3am: the dispatcher deliberately does not emit
# ConsumerLag when lag is unknown (consumer not started, no partition
# assignment, or the Kafka query errored) rather than emitting a fabricated 0.
# So a *dead* consumer shows up as absent datapoints, not as a high value, and
# notBreaching keeps this alarm quiet for it. That case belongs to the ECS
# task-count alarm and to worker supervision, not here.
#
# The MCP read surface makes the same distinction explicitly, so an agent
# reading JSON cannot mistake it either: `get_consumer_lag` returns
# `lag_known: false` with `lag: null` for the unknown case and never a
# fabricated 0 (R2-17). Absent-is-not-zero holds on both the metric and the
# tool; if one side ever starts emitting a placeholder, fix that side rather
# than teaching the other to expect it.

resource "aws_cloudwatch_metric_alarm" "queue_depth" {
  alarm_name          = "${var.app_name}-queue-depth-high"
  alarm_description   = "Job backlog above 50 (Kafka consumer lag). Runbook: rb-queue-depth-high (/admin/runbooks/rb-queue-depth-high). Worker throughput may be insufficient."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "ConsumerLag"
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
# the error budget in about 100 minutes. The math: at a 1.0× burn rate the
# whole budget lasts the full window; the canonical fast-burn threshold is
# 14.4× over a 1h sliding window. `job_completion_rate` (app/services/slo.py)
# targets 99% over a rolling 24h window, so budget = 1% and the fast-burn
# failure-rate threshold = 14.4 * 0.01 = 0.144 — i.e. 14.4% of jobs in the last
# hour dead-lettering. Exhaustion time is window/burn = 24h / 14.4 ≈ 1h40m.
#
# (This comment previously described a 30-day budget. Both SLOs are declared
# over rolling 24h, and 14.4× against a 30-day budget would take ~50h to
# exhaust it, not the "<2h" the comment claimed. The threshold was always
# right for the real 24h window; only its stated basis was wrong.)
#
# Implemented as a math expression on the two custom metrics. CloudWatch
# doesn't have a ratio aggregation natively, so we compute it here.
#
# Both legs are SUM(SEARCH(...)) rather than plain metric references because
# the dispatcher only ever emits JobDeadLettered/JobCompleted with a JobType
# dimension. A dimensionless metric reference is a *different* metric to
# CloudWatch, not an aggregate of the dimensioned ones, so the previous shape
# had no data source at all and — with notBreaching — sat in OK permanently.
# SEARCH over the {IncidentPlatform,JobType} schema matches every job type
# including ones added later, and SUM collapses the multiple series into the
# single one an alarm requires. Rejected alternative: adding a JobType
# dimension to the alarm, which yields one alarm per type and destroys the
# cross-type ratio this SLO is defined on.
#
# Known gap: if an hour produces dead-letters and zero completions, the
# JobCompleted search returns no series and the expression yields no data
# rather than 1.0, so the total-failure case is caught by the backlog and
# outbox alarms instead of here. Fixing that properly needs a dimensionless
# rollup counter on the emitter side (backend/app/core/metrics.py), which is
# out of scope for an infra-only change.

resource "aws_cloudwatch_metric_alarm" "slo_job_completion_fast_burn" {
  alarm_name          = "${var.app_name}-slo-job-completion-fast-burn"
  alarm_description   = "Job-completion SLO burning at 14.4× normal — will exhaust the 24h error budget in ~100 min. Runbook: rb-slo-job-completion (/admin/runbooks/rb-slo-job-completion)."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0.144
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "failure_rate"
    expression  = "IF((dl + ok) > 0, dl / (dl + ok), 0)"
    label       = "Job dead-letter rate (all job types)"
    return_data = true
  }

  metric_query {
    id         = "dl"
    expression = "SUM(SEARCH('{IncidentPlatform,JobType} MetricName=\"JobDeadLettered\"', 'Sum', 3600))"
    label      = "Dead-lettered jobs, summed across JobType"
    period     = 3600
  }

  metric_query {
    id         = "ok"
    expression = "SUM(SEARCH('{IncidentPlatform,JobType} MetricName=\"JobCompleted\"', 'Sum', 3600))"
    label      = "Completed jobs, summed across JobType"
    period     = 3600
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

# ── Alarm 7: Job-dispatch latency SLO fast-burn ──────────────────────────────
# `job_dispatch_latency` (app/services/slo.py) targets 95% of jobs dispatched
# within 30s, over a rolling 24h window.
#
# Approximation: if consumer lag stays > 100 for 15 min, we're not meeting the
# 30-second dispatch SLO at any reasonable arrival rate. A precise version
# would compute per-job started_at - created_at via CloudWatch Metric Math,
# but the backlog is a good cheap proxy for it.
#
# Reads ConsumerLag for the same reason as alarm 5: this alarm was wired to
# QueueDepth, which is the Redis delayed-retry set and not the pending-job
# backlog. Time-to-dispatch is a function of how far behind the Kafka consumer
# is, which is precisely what ConsumerLag measures; the delayed set says
# nothing about it. See alarm 5 for the "lag is absent when unknown" caveat.

resource "aws_cloudwatch_metric_alarm" "slo_dispatch_latency_fast_burn" {
  alarm_name          = "${var.app_name}-slo-dispatch-latency-fast-burn"
  alarm_description   = "Job-dispatch latency SLO burning fast — the job backlog is growing. Runbook: rb-slo-dispatch-latency (/admin/runbooks/rb-slo-dispatch-latency)."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "ConsumerLag"
  namespace           = "IncidentPlatform"
  period              = 300
  statistic           = "Average"
  threshold           = 100
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

# ── Alarm 8: Outbox relay stalled ────────────────────────────────────────────
# The relay leader emits OutboxOldestUnpublishedAgeSeconds every ~60s. Age,
# not depth, is the stall signal: a busy system can hold hundreds of rows for
# a second each and be perfectly healthy, while a stalled one can hold three
# rows forever.
#
# This alarm exists because nothing else can see this failure. QueueDepth
# measures the Redis delayed set — it is untouched by the outbox and reads
# green through a total delivery stall. Every lifecycle event (SSE, audit,
# read model, sagas, triage) rides the outbox, so a stall is invisible to
# users right up until nothing in the product updates.
#
# 300s is ~300 relay ticks. Anything that old is stuck, not slow.

resource "aws_cloudwatch_metric_alarm" "outbox_relay_stalled" {
  alarm_name          = "${var.app_name}-outbox-relay-stalled"
  alarm_description   = "Oldest unpublished outbox row is over 5 minutes old — the relay is not draining. Runbook: rb-outbox-relay-stalled (/admin/runbooks/rb-outbox-relay-stalled). No lifecycle events are reaching Kafka."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "OutboxOldestUnpublishedAgeSeconds"
  namespace           = "IncidentPlatform"
  period              = 60
  statistic           = "Maximum"
  threshold           = 300
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

# ── Alarm 9: Outbox rows being dead-lettered ─────────────────────────────────
# A dead-lettered row is an event that will never be delivered — a job whose
# completion no consumer will ever see. The relay is healthy (this is the
# mechanism working as designed), but the events are gone unless someone
# requeues them, so it must not be silent.
#
# treat_missing_data = "notBreaching" because the counter is only emitted
# when it happens; steady state is no datapoints at all. Threshold 0 over a
# 5-minute Sum: any dead-letter at all is worth a look.

resource "aws_cloudwatch_metric_alarm" "outbox_dead_lettered" {
  alarm_name          = "${var.app_name}-outbox-dead-lettered"
  alarm_description   = "The outbox relay abandoned one or more events. Runbook: rb-outbox-relay-stalled (/admin/runbooks/rb-outbox-relay-stalled), 'dead-lettered rows' section. These events will never reach Kafka unless requeued."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "OutboxDeadLettered"
  namespace           = "IncidentPlatform"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}
