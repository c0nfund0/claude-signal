terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }

  # Local state for now. Once this project is stable, migrate to remote state, e.g.:
  # backend "s3" {
  #   bucket         = "claude-signal-tfstate"
  #   key            = "claude-signal/terraform.tfstate"
  #   region         = "eu-north-1"
  #   dynamodb_table = "claude-signal-tf-locks"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.name
      ManagedBy = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
