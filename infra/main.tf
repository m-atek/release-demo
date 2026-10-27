# The demo platform on AWS. Run it through scripts/tf.sh, which signs in with
# your short-lived credentials and points the backend at the state bucket.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.69.0" }
    random  = { source = "hashicorp/random", version = "~> 3.9.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.8.0" }
  }
  # The bucket comes from SSM at init: scripts/tf.sh init.
  backend "s3" {
    key          = "release-demo/main.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", ManagedBy = "terraform" }
  }
}

variable "budget_email" {
  description = "Who gets the budget alert. Set in infra/local.auto.tfvars, which git ignores."
  type        = string
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  services   = yamldecode(file("${path.module}/../services.yaml"))

  parameters = yamldecode(file("${path.module}/parameters.yaml"))
  # Names only: values come from scripts/secrets.py, the setup scripts or the host.
  placeholders = { for name, p in local.parameters : name => p if p.source != "terraform" }
}
