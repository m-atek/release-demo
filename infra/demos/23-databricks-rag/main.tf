# Demo 23: the runbook assistant's lasting objects in Databricks, and the AWS
# side of its pointers. Its own root and state. Terraform makes what costs
# nothing while idle; scripts/demos/rag/search.py makes and deletes what bills
# by the hour, the AI Search endpoint and its indexes, each recording day.
#
# The databricks provider signs in with your own login, the CLI profile
# release-demo, which scripts/demos/rag/tf.sh sets, as demo 5 uses az and
# gcloud. Run it through tf.sh, or bootstrap.sh the first time.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws        = { source = "hashicorp/aws", version = "~> 6.69.0" }
    databricks = { source = "databricks/databricks", version = "~> 1.137.0" }
  }
  backend "s3" {
    key          = "release-demo/demos/23-databricks-rag.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "23-databricks-rag", ManagedBy = "terraform" }
  }
}

# Host and login from the profile in DATABRICKS_CONFIG_PROFILE.
provider "databricks" {}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Written by bootstrap.sh from the workspace: express setup names the catalog.
data "aws_ssm_parameter" "catalog" {
  name = "/release-demo/databricks/catalog"
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  parameters = yamldecode(file("${path.module}/../../parameters.yaml"))
  service    = yamldecode(file("${path.module}/../../../services.yaml"))["runbook-assistant"]
  databricks = local.service.databricks
  catalog    = nonsensitive(data.aws_ssm_parameter.catalog.value)
  pointers   = [for env in values(local.service.environments) : env.pointer]
}
