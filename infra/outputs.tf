output "alb_dns_name" {
  description = "Public DNS name of the Application Load Balancer"
  value       = aws_lb.main.dns_name
}

output "backend_ecr_url" {
  description = "ECR repository URL for the backend image"
  value       = aws_ecr_repository.backend.repository_url
}

output "frontend_ecr_url" {
  description = "ECR repository URL for the frontend image"
  value       = aws_ecr_repository.frontend.repository_url
}

output "ecs_cluster_name" {
  description = "ECS cluster name — used by CI to trigger deployments"
  value       = aws_ecs_cluster.main.name
}

output "backend_service_name" {
  description = "ECS service name for the backend"
  value       = aws_ecs_service.backend.name
}

output "frontend_service_name" {
  description = "ECS service name for the frontend"
  value       = aws_ecs_service.frontend.name
}

output "rds_endpoint" {
  description = "RDS instance endpoint (host:port)"
  value       = aws_db_instance.main.endpoint
  sensitive   = true
}
