# Demo 9, the agent had production access: the coding agent's own AWS role,
# release-demo-coding-agent, with its own root and state. Video 16's job
# agent-task assumes the same role from the tools host. Run it through
# scripts/demos/agent-access/tf.sh, which signs in with your short-lived
# credentials and points the backend at the state bucket of infra/.
#
# IAM roles cost nothing. Nothing here runs by the hour.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.69.0" }
  }
  # The bucket comes from SSM at init: scripts/demos/agent-access/tf.sh init.
  backend "s3" {
    key          = "release-demo/demos/9-agent-access.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "9-agent-access", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  orders     = yamldecode(file("${path.module}/../../../services.yaml"))["orders-service"]
  staging    = local.orders.environments.staging.function
}
