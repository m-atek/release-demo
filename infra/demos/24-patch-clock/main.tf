# Video 24, the patch clock: one read-only role the tools host assumes when /sbom
# reads what is live. Its own root and state; run it through
# scripts/demos/patch_clock/tf.sh, which signs in with your short-lived credentials
# and points the backend at the state bucket of infra/. infra/tools_host.tf lets
# the host assume the role.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.69.0" }
  }
  # The bucket comes from SSM at init: scripts/demos/patch_clock/tf.sh init.
  backend "s3" {
    key          = "release-demo/demos/24-patch-clock.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "24-patch-clock", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  # Every production function services.yaml's live: blocks name.
  functions = sort([
    for service in yamldecode(file("${path.module}/../../../services.yaml")) : service.live.function
    if can(service.live.function)
  ])
}

# Assumed by the tools host's role only, as the evidence reader is.
data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = ["arn:aws:iam::${local.account_id}:role/release-demo-tools-host"]
    }
  }
}

# What is live, and nothing else: no code, no logs, and nothing that writes.
data "aws_iam_policy_document" "read" {
  statement {
    sid     = "ReadWhatIsLive"
    actions = ["lambda:GetAlias", "lambda:GetFunctionConfiguration"]
    resources = flatten([
      for name in local.functions : [
        "arn:aws:lambda:${local.region}:${local.account_id}:function:${name}",
        "arn:aws:lambda:${local.region}:${local.account_id}:function:${name}:*",
      ]
    ])
  }
}

resource "aws_iam_role" "sbom_reader" {
  name                 = "release-demo-sbom-reader"
  description          = "Reads the live alias and configuration of the production functions /sbom searches. Writes nothing."
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "read" {
  name   = "read-what-is-live"
  role   = aws_iam_role.sbom_reader.id
  policy = data.aws_iam_policy_document.read.json
}

output "role_arn" {
  description = "The role scripts/demos/patch_clock/live.py assumes."
  value       = aws_iam_role.sbom_reader.arn
}
