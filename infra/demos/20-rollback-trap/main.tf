# Demo 20, the rollback trap: what orders-service's database needs from AWS,
# with its own root and state. The database itself is a Neon Free project,
# which scripts/demos/rollback-trap/neon.py drives through Neon's API. Run it
# through scripts/demos/rollback-trap/tf.sh, which signs in with your
# short-lived credentials and points the backend at the state bucket of infra/.
#
# The functions, their roles and the deploy role belong to infra/; this root
# only adds inline policies to those roles.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.69.0" }
  }
  # The bucket comes from SSM at init: scripts/demos/rollback-trap/tf.sh init.
  backend "s3" {
    key          = "release-demo/demos/20-rollback-trap.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "20-rollback-trap", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  orders     = yamldecode(file("${path.module}/../../../services.yaml"))["orders-service"]
  parameter  = "arn:aws:ssm:${local.region}:${local.account_id}:parameter/release-demo/orders-service"
}
