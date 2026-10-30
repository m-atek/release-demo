# The evidence reader: one read-only role the tools host assumes to read what
# AWS itself recorded about orders-service. The auditor sample (video 11), the
# AI notes (videos 12 and 13) and the cost check (video 18) share it. Its own
# root and state; run it through scripts/demos/ai_notes/tf.sh, which signs in
# with your short-lived credentials and points the backend at the state
# bucket of infra/. infra/tools_host.tf lets the host assume the role.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.69.0" }
  }
  # The bucket comes from SSM at init: scripts/demos/ai_notes/tf.sh init.
  backend "s3" {
    key          = "release-demo/demos/evidence-reader.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "evidence-reader", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  orders     = yamldecode(file("${path.module}/../../../services.yaml"))["orders-service"]
  functions  = [for environment in local.orders.environments : environment.function]
}

# Assumed by the tools host's role only.
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

# Reads only: the functions, their versions and aliases; CloudTrail's event
# history; the two log groups. Nothing that writes, and no function's code
# other than through GetFunction's short-lived link, which evidence.py uses to
# read a version's own build_info.json.
data "aws_iam_policy_document" "read" {
  statement {
    sid = "ReadOrdersServiceFunctions"
    actions = [
      "lambda:GetFunctionConfiguration",
      "lambda:GetFunction",
      "lambda:GetAlias",
      "lambda:ListVersionsByFunction",
    ]
    resources = flatten([
      for name in local.functions : [
        "arn:aws:lambda:${local.region}:${local.account_id}:function:${name}",
        "arn:aws:lambda:${local.region}:${local.account_id}:function:${name}:*",
      ]
    ])
  }
  statement {
    sid       = "ReadCloudTrailEventHistory"
    actions   = ["cloudtrail:LookupEvents"]
    resources = ["*"]
  }
  statement {
    sid     = "ReadOrdersServiceLogs"
    actions = ["logs:FilterLogEvents", "logs:DescribeLogStreams"]
    resources = flatten([
      for name in local.functions : [
        "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${name}",
        "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${name}:*",
      ]
    ])
  }
}

resource "aws_iam_role" "evidence_reader" {
  name                 = "release-demo-evidence-reader"
  description          = "Reads orders-service's Lambda versions, CloudTrail events and logs. Writes nothing."
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "read" {
  name   = "read-orders-service-records"
  role   = aws_iam_role.evidence_reader.id
  policy = data.aws_iam_policy_document.read.json
}

output "role_arn" {
  description = "The role scripts/evidence.py and the cost check assume."
  value       = aws_iam_role.evidence_reader.arn
}
