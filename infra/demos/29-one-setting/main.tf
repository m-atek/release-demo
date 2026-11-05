# Video 29, one setting, no release: orders-service's settings file in AWS
# AppConfig, with AWS's validator on it. Its own application, so that demo 21's
# flags, its reset and its event bridge never see a setting: AWS AppConfig lists
# deployments per environment, whatever the profile. Its own root and state; run
# it through scripts/demos/settings/tf.sh, which signs in with your short-lived
# credentials and points the backend at the state bucket of infra/.
#
# The functions and the tools host belong to infra/, and the alarm and the role
# that reads it to infra/demos/21-release-train-flags, which must be applied
# first. This root adds inline policies to the functions' and the host's roles,
# so each reads or deploys only this application's file.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.69.0" }
  }
  # The bucket comes from SSM at init: scripts/demos/settings/tf.sh init.
  backend "s3" {
    key          = "release-demo/demos/29-one-setting.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "29-one-setting", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  orders     = yamldecode(file("${path.module}/../../../services.yaml"))["orders-service"]
  settings   = local.orders.app_settings
  alarm_arn  = "arn:aws:cloudwatch:${local.region}:${local.account_id}:alarm:${local.settings.alarm}"
}
