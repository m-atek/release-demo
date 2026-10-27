# Demo 1: the store fleet. Six Windows Server 2022 instances, STORE-001 to
# STORE-006, that the release pipeline upgrades in waves through SSM Run
# Command. Its own root and state; run it through scripts/demos/windows/tf.sh,
# or scripts/demo.sh up windows, which also bootstraps the fleet.
#
# It reads the shared platform (VPC, subnets, the tools host) and adds only its
# own resources, plus one rule on the tools host's security group: JCR's port
# 8082, open to the fleet's security group only.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.69.0" }
  }
  # The bucket comes from SSM at init: scripts/demos/windows/tf.sh init.
  backend "s3" {
    key          = "release-demo/demos/1-windows.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "1-windows", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  service    = yamldecode(file("${path.module}/../../../services.yaml"))["store-sync"]
  fleet      = local.service.environments.production
  # STORE-001 in subnet a, STORE-002 in b, and so on: two availability zones.
  servers = { for index, name in sort(keys(local.fleet.servers)) : name => {
    store  = local.fleet.servers[name].store
    subnet = index % 2 == 0 ? "a" : "b"
  } }
  token_parameter = "jcr/store-fleet-token"
}

# The shared platform, read only.
data "aws_vpc" "demo" {
  tags = { Name = "release-demo" }
}

data "aws_subnet" "public" {
  for_each = toset(["a", "b"])
  vpc_id   = data.aws_vpc.demo.id
  tags     = { Name = "release-demo-public-${each.key}" }
}

data "aws_security_group" "tools_host" {
  vpc_id = data.aws_vpc.demo.id
  name   = "release-demo-tools-host"
}

data "aws_iam_role" "tools_host" {
  name = "release-demo-tools-host"
}
