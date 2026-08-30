locals {
  azs           = ["${var.aws_region}a", "${var.aws_region}b"]
  public_cidrs  = ["10.0.0.0/24", "10.0.1.0/24"]
  private_cidrs = ["10.0.2.0/24", "10.0.3.0/24"]
}

# ── VPC ───────────────────────────────────────────────────────────────────────

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
}

# ── Public subnets (ALB + ECS tasks) ─────────────────────────────────────────
# NOTE: ECS tasks are placed in public subnets with assign_public_ip=ENABLED
# so they can pull images from ECR without a NAT gateway (saves ~$45/month).
# For stricter production use, move ECS to private subnets + NAT gateway or ECR VPC endpoints.

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = local.public_cidrs[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true
}

# ── Private subnets (RDS + ElastiCache) ──────────────────────────────────────

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.private_cidrs[count.index]
  availability_zone = local.azs[count.index]
}

# ── Internet Gateway ──────────────────────────────────────────────────────────

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# ── Security Groups ───────────────────────────────────────────────────────────

resource "aws_security_group" "alb" {
  name        = "${var.app_name}-alb"
  description = "Allow HTTP/HTTPS from anywhere"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "backend" {
  name        = "${var.app_name}-backend"
  description = "Allow traffic from ALB to backend on port 8000"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "frontend" {
  name        = "${var.app_name}-frontend"
  description = "Allow traffic from ALB to frontend on port 80"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# The MCP standalone process (ADR 0006 — its own deployable, same image,
# different command). Its own security group rather than sharing the
# backend's: the two services front different audiences (agent traffic vs
# human traffic), and giving them one group would mean any future rule
# written for one silently applies to the other.
resource "aws_security_group" "mcp" {
  name        = "${var.app_name}-mcp"
  description = "Allow traffic from ALB to the MCP server on port 8001"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 8001
    to_port         = 8001
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "rds" {
  name        = "${var.app_name}-rds"
  description = "Allow Postgres from the backend and MCP tasks only"
  vpc_id      = aws_vpc.main.id

  # Both application deployables. The MCP handlers call the service layer
  # directly (ADR 0006), so the process holds real database sessions — as
  # the non-owner incident_app role, like the backend runtime.
  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.backend.id, aws_security_group.mcp.id]
  }
}

resource "aws_security_group" "redis" {
  name        = "${var.app_name}-redis"
  description = "Allow Redis from the backend and MCP tasks only"
  vpc_id      = aws_vpc.main.id

  # The MCP surface reads and writes Redis on the same paths the REST
  # surface does: per-principal rate limits, the job cache, the chaos
  # keys where CHAOS_ENABLED permits them.
  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.backend.id, aws_security_group.mcp.id]
  }
}
