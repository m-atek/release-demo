# Demo 14, GitHub Actions: what lets .github/workflows/release.yml's jobs sign
# in to AWS with GitHub's OIDC token, and nothing more. GitHub stores no keys:
# the OIDC provider for token.actions.githubusercontent.com, and three roles,
# each trusting one token subject (roles.tf). Plus the key the jobs encrypt
# the run state they pass on with, since a public repository's artifacts are
# public. Its own root and state; run it through
# scripts/demos/github_actions/tf.sh.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 6.69.0" }
    random = { source = "hashicorp/random", version = "~> 3.9.0" }
  }
  # The bucket comes from SSM at init: scripts/demos/github_actions/tf.sh init.
  backend "s3" {
    key          = "release-demo/demos/14-github-actions.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "14-github-actions", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# owner/release-demo, which scripts/github_app.py wrote: the subjects name it.
data "aws_ssm_parameter" "repository" {
  name = "/release-demo/github/repository"
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  repository = nonsensitive(data.aws_ssm_parameter.repository.value)
  orders     = yamldecode(file("${path.module}/../../../services.yaml"))["orders-service"]
  parameters = yamldecode(file("${path.module}/../../parameters.yaml"))
}

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

resource "random_password" "run_state_key" {
  length  = 48
  special = false
}

resource "aws_ssm_parameter" "run_state_key" {
  name        = "/release-demo/github/run-state-key"
  type        = "SecureString"
  value       = random_password.run_state_key.result
  description = substr(local.parameters["github/run-state-key"].help, 0, 1024)
}

output "roles" {
  description = "The role each job of release.yml signs in as, by the token subject it trusts."
  value       = { for key, role in aws_iam_role.github : key => role.arn }
}
