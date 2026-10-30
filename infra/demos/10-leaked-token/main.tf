# Demo 10, a leaked token revoked against the clock: what the tools host's
# role needs beyond infra/ to rotate the platform's tokens by script, with
# its own root and state. scripts/rotate.py jcr writes jcr/token, which the
# host may write already (infra/tools_host.tf); scripts/rotate.py sonar
# writes sonar/token, which this allows. Run it through
# scripts/demos/leaked-token/tf.sh. An inline policy costs nothing.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.69.0" }
  }
  # The bucket comes from SSM at init: scripts/demos/leaked-token/tf.sh init.
  backend "s3" {
    key          = "release-demo/demos/10-leaked-token.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "10-leaked-token", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
}

data "aws_iam_policy_document" "rotate_sonar_token" {
  statement {
    sid       = "WriteRotatedSonarToken"
    actions   = ["ssm:PutParameter"]
    resources = ["arn:aws:ssm:${local.region}:${local.account_id}:parameter/release-demo/sonar/token"]
  }
}

# Beside the role's own policy from infra/, under its own name.
resource "aws_iam_role_policy" "rotate_sonar_token" {
  name   = "demo-10-rotate-sonar-token"
  role   = "release-demo-tools-host"
  policy = data.aws_iam_policy_document.rotate_sonar_token.json
}
