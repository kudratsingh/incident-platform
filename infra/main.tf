terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }

  # Uncomment after bootstrapping: create the S3 bucket and DynamoDB table manually first.
  # backend "s3" {
  #   bucket         = "incident-platform-tf-state"
  #   key            = "incident-platform/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "incident-platform-tf-locks"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.app_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
