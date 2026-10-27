# Demo 4, APIs: pricing-api on Lambda behind an API Gateway HTTP API, with
# its own root and state. Run it through scripts/demos/apis/tf.sh, which signs
# in with your short-lived credentials and points the backend at the state
# bucket of infra/.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.69.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.8.0" }
  }
  # The bucket comes from SSM at init: scripts/demos/apis/tf.sh init.
  backend "s3" {
    key          = "release-demo/demos/4-apis.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "4-apis", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  pricing    = yamldecode(file("${path.module}/../../../services.yaml"))["pricing-api"]
}
