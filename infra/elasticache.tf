resource "aws_elasticache_subnet_group" "main" {
  name       = var.app_name
  subnet_ids = aws_subnet.private[*].id
}

# ElastiCache AUTH token. Constraints: 16-128 printable chars, no '@', '"',
# or '/'. special=false (alphanumeric only) satisfies that; random_password.db
# cannot be reused here — its override_special includes forbidden characters.
resource "random_password" "redis_auth" {
  length  = 32
  special = false
}

# A replication group, not aws_elasticache_cluster: the bare cluster resource
# cannot express in-transit encryption for Redis. Single-node (num_cache_clusters
# = 1) keeps the old cluster's footprint; both encryption flags match the
# at-rest posture already applied to RDS and S3.
resource "aws_elasticache_replication_group" "redis" {
  replication_group_id = var.app_name
  description          = "Redis for ${var.app_name} (queues, cache, pub/sub)"
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = "cache.t3.micro"
  num_cache_clusters   = 1
  parameter_group_name = "default.redis7"
  port                 = 6379

  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = [aws_security_group.redis.id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = random_password.redis_auth.result
}
