# Demo 3: the two cloud servers for the inventory service, inv-cloud-01 and
# inv-cloud-02, and what reaches them. Its own root and state; it reads the
# platform's VPC and tools host but changes neither, except for one ingress
# rule on the tools host's security group: JCR's port 8082, from these
# servers only, as the platform spec plans.
#
# The servers have no inbound rules. Ansible reaches them through Systems
# Manager (amazon.aws.aws_ssm, files through the S3 bucket below), and they
# fetch packages from JCR over the VPC with a read-only token from SSM. Each
# stops itself 4 hours after boot.
#
# Run it through scripts/demos/linux/up.sh, which inits the backend from SSM
# like scripts/tf.sh: terraform -chdir=infra/demos/3-linux init -backend-config=bucket=...

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 6.69.0" }
    random = { source = "hashicorp/random", version = "~> 3.9.0" }
  }
  backend "s3" {
    key          = "release-demo/demos/3-linux.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "3-linux", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  service    = yamldecode(file("${path.module}/../../../services.yaml"))["inventory"]
  parameters = yamldecode(file("${path.module}/../../parameters.yaml"))
  # The cloud servers from services.yaml, each in its own subnet.
  cloud_servers = {
    for i, s in [for s in local.service.environments.production.servers : s if s.site == "cloud"] :
    s.name => { subnet = ["a", "b"][i % 2] }
  }
  ssm_bucket = "release-demo-inventory-ssm-${local.account_id}"
}

# The platform's network and tools host, read only.
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

data "aws_ssm_parameter" "ubuntu_ami" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}
