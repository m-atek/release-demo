# The network stack of demo 2. Every change to it arrives as a pull request:
# the checks, a plan anyone can read, its monthly cost, a ServiceNow change,
# and after approval an apply of exactly that plan (../Jenkinsfile).

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.69.0" }
  }
  # The bucket comes from SSM at init: scripts/demos/terraform/stack.py.
  backend "s3" {
    key          = "release-demo/demos/2-terraform/network.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = {
      Project   = "release-demo"
      Demo      = "2-terraform"
      Owner     = "platform-team"
      ManagedBy = "terraform"
    }
  }
}
