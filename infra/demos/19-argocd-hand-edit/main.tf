# Demo 19: the cluster host. A t3.large of its own runs k3s, Argo CD and
# production's catalog pods, because Argo CD doesn't fit demo 8's 2 GB on the
# tools host. Its own root and state; it reads the platform's VPC, subnet,
# tools host and parameters, and changes nothing of theirs except one ingress
# rule on the tools host's security group: JCR's port 8082, from this host,
# as demos 1 and 3 add theirs.
#
# The host has no inbound rule from outside the VPC. The tools host reaches
# Argo CD on its NodePort 30080 (cloudflared, the fix pipeline, reset.py);
# the Mac reaches the Kubernetes API through an SSM port forward only. It
# stops itself after 30 idle minutes; scripts/demo.sh up argocd starts it.
#
# Run it through scripts/demos/argocd/tf.sh, which inits the backend from SSM
# like scripts/tf.sh. Apply infra/ first: it makes the host's address.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.69.0" }
  }
  backend "s3" {
    key          = "release-demo/demos/19-argocd-hand-edit.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "19-argocd-hand-edit", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  parameters = yamldecode(file("${path.module}/../../parameters.yaml"))
  # What the host reads in SSM, and the one value it writes there.
  reads  = ["cluster-host/*", "bridge/events-token", "jcr/inventory-read-token"]
  writes = ["argocd/pipeline-token"]
}

# The platform's network and tools host, read only.
data "aws_vpc" "demo" {
  tags = { Name = "release-demo" }
}

data "aws_subnet" "a" {
  vpc_id = data.aws_vpc.demo.id
  tags   = { Name = "release-demo-public-a" }
}

data "aws_security_group" "tools_host" {
  vpc_id = data.aws_vpc.demo.id
  name   = "release-demo-tools-host"
}

data "aws_instance" "tools_host" {
  filter {
    name   = "tag:Name"
    values = ["release-demo-tools-host"]
  }
  # A replaced tools host stays listed as terminated for about an hour.
  filter {
    name   = "instance-state-name"
    values = ["pending", "running", "stopping", "stopped"]
  }
}

data "aws_ssm_parameter" "ubuntu_ami" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

data "aws_ssm_parameter" "platform" {
  for_each = toset(["github/repository", "cluster-host/address", "bridge/url"])
  name     = "/release-demo/${each.key}"
}
