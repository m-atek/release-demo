# Demo 18, the cost check: the store orders-service 1.10.1 keeps each order's
# delivery message in, one table per environment, and what the functions may
# read and write for 1.10.0 and 1.10.1. Its own root and state; run it through
# scripts/demos/cost_gate/tf.sh, which signs in with your short-lived
# credentials and points the backend at the state bucket of infra/.
#
# The functions and their roles are infra/'s (orders_service.tf). Versions
# before 1.10.0 neither read the key nor touch the tables.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.69.0" }
  }
  # The bucket comes from SSM at init: scripts/demos/cost_gate/tf.sh init.
  backend "s3" {
    key          = "release-demo/demos/18-cost-gate.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "18-cost-gate", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  region       = data.aws_region.current.region
  environments = yamldecode(file("${path.module}/../../../services.yaml"))["orders-service"].environments
  # 1.10.0 and 1.10.1 read the key from here, once per cold start.
  key_parameter = "arn:aws:ssm:${local.region}:${local.account_id}:parameter/release-demo/anthropic/api-key"
}

# One message per order, written once and kept 30 days (1.10.1). On demand:
# idle, the tables cost only their few kilobytes of storage.
resource "aws_dynamodb_table" "messages" {
  for_each     = local.environments
  name         = "orders-service-messages-${each.key}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "order_id"

  attribute {
    name = "order_id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

data "aws_iam_role" "function" {
  for_each = local.environments
  name     = "${each.value.function}-function"
}

# Each function: its own table, the Anthropic key, and KMS only through SSM.
data "aws_iam_policy_document" "function" {
  for_each = local.environments

  statement {
    sid       = "OwnMessagesTable"
    actions   = ["dynamodb:BatchGetItem", "dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [aws_dynamodb_table.messages[each.key].arn]
  }
  statement {
    sid       = "AnthropicKey"
    actions   = ["ssm:GetParameter"]
    resources = [local.key_parameter]
  }
  statement {
    sid       = "DecryptTheKeyThroughSsm"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${local.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "function" {
  for_each = local.environments
  name     = "cost-gate-messages-and-key"
  role     = data.aws_iam_role.function[each.key].id
  policy   = data.aws_iam_policy_document.function[each.key].json
}

output "tables" {
  description = "The messages table of each environment."
  value       = { for name, table in aws_dynamodb_table.messages : name => table.name }
}
