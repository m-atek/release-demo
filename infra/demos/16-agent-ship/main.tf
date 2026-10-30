# Demo 16, the agent wrote the code but can't ship it: what the tools host's
# role needs so the Jenkins job agent-task can give the coding agent its AWS
# session, with its own root and state. The role release-demo-coding-agent
# itself is video 9's (infra/demos/9-agent-access), which already trusts the
# tools host's role. Run it through scripts/demos/agent_ship/tf.sh. An inline
# policy costs nothing.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.69.0" }
  }
  # The bucket comes from SSM at init: scripts/demos/agent_ship/tf.sh init.
  backend "s3" {
    key          = "release-demo/demos/16-agent-ship.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "16-agent-ship", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
}

# The host may assume the coding agent's role, and only with the session
# name coding-agent, so CloudTrail names every call the agent makes.
data "aws_iam_policy_document" "assume_coding_agent" {
  statement {
    sid       = "AssumeTheCodingAgent"
    actions   = ["sts:AssumeRole"]
    resources = ["arn:aws:iam::${local.account_id}:role/release-demo-coding-agent"]
    condition {
      test     = "StringEquals"
      variable = "sts:RoleSessionName"
      values   = ["coding-agent"]
    }
  }
}

resource "aws_iam_role_policy" "assume_coding_agent" {
  name   = "demo-16-assume-coding-agent"
  role   = "release-demo-tools-host"
  policy = data.aws_iam_policy_document.assume_coding_agent.json
}
