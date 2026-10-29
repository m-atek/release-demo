# Demo 2, infrastructure changes with Terraform: the two roles the pipeline
# plans and applies the network stack in network/ with, and what starts the
# drift check after a change made outside Terraform. Applied from the Mac
# with your short-lived credentials (scripts/demo.sh up terraform), never by
# the pipeline, so a pull request can't widen its own permissions.
#
# - release-demo-deploy-network-plan reads the stack and its state: the plan
#   on every pull request, and the drift check.
# - release-demo-deploy-network-apply changes the stack's own resources only,
#   after the change is approved in ServiceNow.
# - drift_trigger.tf and trail.tf: CloudTrail, an EventBridge rule and a
#   Lambda function that start the drift check a minute or two after someone
#   changes the stack's security groups or buckets outside Terraform.
#
# The tools host assumes either role (its policy allows release-demo-deploy-*).
# So can the account's administrators, to run the same steps from the Mac.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.69.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.8.0" }
  }
  # The bucket comes from SSM at init: scripts/demos/terraform/stack.py.
  backend "s3" {
    key          = "release-demo/demos/2-terraform.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "2-terraform", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}

data "aws_ssm_parameter" "state_bucket" {
  name = "/release-demo/terraform/state-bucket"
}

data "aws_iam_role" "tools_host" {
  name = "release-demo-tools-host"
}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  state_bucket = "arn:aws:s3:::${nonsensitive(data.aws_ssm_parameter.state_bucket.value)}"
  state_object = "${local.state_bucket}/release-demo/demos/2-terraform/network.tfstate"
  # The stack's resources, by name. bucket_prefix adds a random suffix.
  exports_bucket_prefix = "release-demo-store-exports-"
  exports_buckets       = ["arn:aws:s3:::${local.exports_bucket_prefix}*", "arn:aws:s3:::${local.exports_bucket_prefix}*/*"]
  exports_queues        = ["arn:aws:sqs:eu-west-1:${local.account_id}:release-demo-store-exports*"]
  ec2_arn               = "arn:aws:ec2:eu-west-1:${local.account_id}"
  demo_tag              = "2-terraform"
}

data "aws_iam_policy_document" "assume" {
  statement {
    sid     = "ToolsHost"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = [data.aws_iam_role.tools_host.arn]
    }
  }
  statement {
    sid     = "AdministratorsOnTheMac"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:PrincipalArn"
      values   = ["arn:aws:iam::${local.account_id}:role/aws-reserved/sso.amazonaws.com/*/AWSReservedSSO_AdministratorAccess_*"]
    }
  }
}

# The plan role: read everything the plan refreshes, and take the state lock.
data "aws_iam_policy_document" "plan" {
  statement {
    sid       = "ReadState"
    actions   = ["s3:GetObject"]
    resources = [local.state_object, "${local.state_object}.tflock"]
  }
  statement {
    sid       = "ListStateBucket"
    actions   = ["s3:ListBucket"]
    resources = [local.state_bucket]
  }
  statement {
    sid       = "TakeTheLock"
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["${local.state_object}.tflock"]
  }
  statement {
    sid       = "ReadNetwork"
    actions   = ["ec2:Describe*"]
    resources = ["*"]
  }
  statement {
    sid       = "ReadExports"
    actions   = ["s3:Get*", "s3:ListBucket", "sqs:GetQueueAttributes", "sqs:GetQueueUrl", "sqs:ListQueueTags"]
    resources = concat(local.exports_buckets, local.exports_queues)
  }
  statement {
    sid       = "ListQueues"
    actions   = ["sqs:ListQueues"]
    resources = ["*"]
  }
}

# The apply role: the plan role's reads, the state itself, and changes to the
# stack's resources only. EC2 resources are the stack's by their Demo tag,
# which the provider sets when it creates them.
data "aws_iam_policy_document" "apply" {
  source_policy_documents = [data.aws_iam_policy_document.plan.json]

  statement {
    sid       = "WriteState"
    actions   = ["s3:PutObject"]
    resources = [local.state_object]
  }
  statement {
    sid     = "CreateTaggedNetwork"
    actions = ["ec2:CreateVpc", "ec2:CreateSubnet", "ec2:CreateSecurityGroup"]
    resources = [
      "${local.ec2_arn}:vpc/*", "${local.ec2_arn}:subnet/*", "${local.ec2_arn}:security-group/*",
    ]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Demo"
      values   = [local.demo_tag]
    }
  }
  statement {
    sid       = "CreateInTheStacksVpc"
    actions   = ["ec2:CreateSubnet", "ec2:CreateSecurityGroup"]
    resources = ["${local.ec2_arn}:vpc/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Demo"
      values   = [local.demo_tag]
    }
  }
  statement {
    sid       = "TagOnCreate"
    actions   = ["ec2:CreateTags"]
    resources = ["${local.ec2_arn}:*/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["CreateVpc", "CreateSubnet", "CreateSecurityGroup"]
    }
  }
  statement {
    sid = "ChangeTheStacksNetwork"
    actions = [
      "ec2:CreateTags", "ec2:DeleteTags",
      "ec2:ModifyVpcAttribute", "ec2:DeleteVpc",
      "ec2:ModifySubnetAttribute", "ec2:DeleteSubnet",
      "ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress",
      "ec2:RevokeSecurityGroupIngress", "ec2:RevokeSecurityGroupEgress",
      "ec2:UpdateSecurityGroupRuleDescriptionsIngress", "ec2:UpdateSecurityGroupRuleDescriptionsEgress",
      "ec2:ModifySecurityGroupRules", "ec2:DeleteSecurityGroup",
    ]
    resources = ["${local.ec2_arn}:*/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Demo"
      values   = [local.demo_tag]
    }
  }
  # A new VPC's default security group comes without tags. Terraform empties
  # it and tags it, which makes it the stack's. Groups that carry a Project tag,
  # such as the platform's own, stay out of reach.
  statement {
    sid       = "EmptyTheDefaultGroup"
    actions   = ["ec2:RevokeSecurityGroupIngress", "ec2:RevokeSecurityGroupEgress"]
    resources = ["${local.ec2_arn}:security-group/*"]
    condition {
      test     = "Null"
      variable = "ec2:ResourceTag/Project"
      values   = ["true"]
    }
  }
  statement {
    sid       = "ClaimTheDefaultGroup"
    actions   = ["ec2:CreateTags"]
    resources = ["${local.ec2_arn}:security-group/*"]
    condition {
      test     = "Null"
      variable = "ec2:ResourceTag/Project"
      values   = ["true"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Demo"
      values   = [local.demo_tag]
    }
  }
  statement {
    sid       = "ChangeExports"
    actions   = ["s3:*", "sqs:*"]
    resources = concat(local.exports_buckets, local.exports_queues)
  }
}

resource "aws_iam_role" "plan" {
  name                 = "release-demo-deploy-network-plan"
  description          = "Demo 2: plans the network stack on pull requests and checks it for drift. Read-only."
  assume_role_policy   = data.aws_iam_policy_document.assume.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "plan" {
  name   = "plan-network-stack"
  role   = aws_iam_role.plan.id
  policy = data.aws_iam_policy_document.plan.json
}

resource "aws_iam_role" "apply" {
  name                 = "release-demo-deploy-network-apply"
  description          = "Demo 2: applies approved plans to the network stack. Changes only the stack's resources."
  assume_role_policy   = data.aws_iam_policy_document.assume.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "apply" {
  name   = "apply-network-stack"
  role   = aws_iam_role.apply.id
  policy = data.aws_iam_policy_document.apply.json
}

output "plan_role" {
  value = aws_iam_role.plan.name
}

output "apply_role" {
  value = aws_iam_role.apply.name
}
