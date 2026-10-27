# Demo 6, desktop apps: the Jamf stand-in, the Windows build agent and test
# instance, and the deploy role the pipeline uses for them. Its own root and
# state; run it through scripts/demos/desktop/tf.sh.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.69.0" }
    random  = { source = "hashicorp/random", version = "~> 3.9.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.8.0" }
  }
  # The bucket comes from SSM at init: scripts/demos/desktop/tf.sh.
  backend "s3" {
    key          = "release-demo/demos/6-desktop.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "6-desktop", ManagedBy = "terraform" }
  }
}

variable "windows" {
  description = "Whether the Windows build agent and test instance exist. scripts/demos/desktop/down.sh --remove sets it false."
  type        = bool
  default     = true
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  parameters = yamldecode(file("${path.module}/../../parameters.yaml"))
  # The bucket for the Jamf stand-in's state and files, and the MSIs on their
  # way to the Windows test instance.
  bucket = "release-demo-desktop-${local.account_id}"
}

# The tools host's role, which assumes the deploy role. Made by infra/.
data "aws_iam_role" "tools_host" {
  name = "release-demo-tools-host"
}

resource "aws_s3_bucket" "desktop" {
  bucket = local.bucket
}

resource "aws_s3_bucket_public_access_block" "desktop" {
  bucket                  = aws_s3_bucket.desktop.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "desktop" {
  bucket = aws_s3_bucket.desktop.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Installers and stand-in uploads are copies: they go after a month.
resource "aws_s3_bucket_lifecycle_configuration" "desktop" {
  bucket = aws_s3_bucket.desktop.id
  rule {
    id     = "installers"
    status = "Enabled"
    filter {
      prefix = "installers/"
    }
    expiration {
      days = 30
    }
  }
  rule {
    id     = "uploads"
    status = "Enabled"
    filter {
      prefix = "files/uploads/"
    }
    expiration {
      days = 2
    }
  }
}

output "bucket" {
  value = aws_s3_bucket.desktop.bucket
}
