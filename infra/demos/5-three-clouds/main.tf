# Demo 5, one release to three clouds: the AWS side, and the Jenkins identity
# provider every cloud trusts (demos 6 and 7 trust it too). Its own root and
# state. Azure and Google Cloud have their own roots next to this one, azure/
# and gcp/, so this one applies without their accounts.
#
# Run it through scripts/demos/clouds/tf.sh aws ..., which signs in with your
# short-lived AWS credentials and points the backend at the state bucket. The
# first time: scripts/demos/clouds/bootstrap.sh aws.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 6.69.0" }
    random = { source = "hashicorp/random", version = "~> 3.9.0" }
  }
  # The bucket comes from SSM at init.
  backend "s3" {
    key          = "release-demo/demos/5-three-clouds.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "5-three-clouds", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  quote      = yamldecode(file("${path.module}/../../../services.yaml"))["quote-service"]

  # Every release runs as the Jenkins job release, so every ID token says so:
  # the plugin's default subject is the job's URL.
  jenkins_subject = "https://jenkins-demo.es.nl/job/release/"
  aws_audience    = "sts.amazonaws.com"
  initial_version = "1.1.0"
}
