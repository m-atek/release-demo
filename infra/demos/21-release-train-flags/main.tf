# Demo 21, the release train and a feature flag: orders-service's flags in AWS
# AppConfig, the alarm that rolls a flag back, the event bridge's AppConfig
# route that records every switch, and the schedule that starts the tools
# host before the train. Its own root and state; run it through
# scripts/demos/release-train/tf.sh, which signs in with your short-lived
# credentials and points the backend at the state bucket of infra/.
#
# The functions, the Slack handler and the tools host belong to infra/; this
# root adds inline policies to their roles, so each reads or deploys only
# this application's flags.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.69.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.8.0" }
  }
  # The bucket comes from SSM at init: scripts/demos/release-train/tf.sh init.
  backend "s3" {
    key          = "release-demo/demos/21-release-train-flags.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "21-release-train-flags", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  orders     = yamldecode(file("${path.module}/../../../services.yaml"))["orders-service"]
  flags      = local.orders.flags
  people     = yamldecode(file("${path.module}/../../../people.yaml"))
  # The flags the profile defines: those of the first version that has them.
  declared      = yamldecode(file("${path.module}/../../../demo-states/orders-service/1.8.0/flags.yaml"))
  alarm_name    = "orders-service-production-errors"
  watch_minutes = 10
  bridge_name   = "release-demo-appconfig-events"
}
