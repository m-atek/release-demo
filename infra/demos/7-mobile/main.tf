# Demo 7, mobile apps: what store-floor needs in AWS. Its own root and state,
# run through scripts/demos/mobile/tf.sh.
#
# - One private bucket: match/ holds the iOS certificates and profiles fastlane
#   match encrypts (read only in CI), standins/ the stand-ins' state.
# - The deploy role the tools host assumes for store-floor: the stand-ins'
#   state only.
# - A read-only role for the Mac agent's match, trusted through demo 5's
#   Jenkins identity provider, once that exists (var.jenkins_oidc_provider_arn).
# - Two plain SSM values: the bucket's name and orders-service's production
#   URL, which the app shows today's orders from.
#
# Nothing here costs money by the hour: a few cents a month of S3.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 6.69.0" }
    random = { source = "hashicorp/random", version = "~> 3.9.0" }
  }
  # The bucket comes from SSM at init: scripts/demos/mobile/tf.sh init.
  backend "s3" {
    key          = "release-demo/demos/7-mobile.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "7-mobile", ManagedBy = "terraform" }
  }
}

variable "jenkins_oidc_provider_arn" {
  description = "Demo 5's IAM OIDC provider for Jenkins' ID tokens. Empty until it exists; then the Mac agent's match role is made."
  type        = string
  default     = ""
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  service    = "store-floor"
}

resource "random_id" "suffix" {
  byte_length = 4
}

resource "aws_s3_bucket" "store_floor" {
  bucket = "release-demo-store-floor-${random_id.suffix.hex}"
}

resource "aws_s3_bucket_public_access_block" "store_floor" {
  bucket                  = aws_s3_bucket.store_floor.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "store_floor" {
  bucket = aws_s3_bucket.store_floor.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "store_floor" {
  bucket = aws_s3_bucket.store_floor.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# match keeps one file per certificate and profile; versions guard them
# against an accidental overwrite.
resource "aws_s3_bucket_versioning" "store_floor" {
  bucket = aws_s3_bucket.store_floor.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "store_floor" {
  bucket = aws_s3_bucket.store_floor.id
  rule {
    id     = "old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

# The deploy role: assumed by the tools host's role only, and able to read and
# write the stand-ins' state, nothing else.
data "aws_iam_policy_document" "deploy_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = ["arn:aws:iam::${local.account_id}:role/release-demo-tools-host"]
    }
  }
}

data "aws_iam_policy_document" "deploy" {
  statement {
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.store_floor.arn}/standins/*"]
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.store_floor.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["standins/*"]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name                 = "release-demo-deploy-${local.service}"
  assume_role_policy   = data.aws_iam_policy_document.deploy_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "deploy" {
  name   = "deploy-${local.service}"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}

# The Mac agent's fastlane match reads the encrypted certificates with this
# role, from the build's Jenkins ID token: no AWS key on the Mac. Read only.
data "aws_iam_policy_document" "match_trust" {
  count = var.jenkins_oidc_provider_arn == "" ? 0 : 1
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [var.jenkins_oidc_provider_arn]
    }
    condition {
      test     = "StringLike"
      variable = "${replace(var.jenkins_oidc_provider_arn, "/^.*:oidc-provider\\//", "")}:sub"
      values   = ["*/job/store-floor-build/*"]
    }
  }
}

data "aws_iam_policy_document" "match" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.store_floor.arn}/match/*"]
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.store_floor.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["match/*"]
    }
  }
}

resource "aws_iam_role" "match" {
  count                = var.jenkins_oidc_provider_arn == "" ? 0 : 1
  name                 = "release-demo-match-read-${local.service}"
  assume_role_policy   = data.aws_iam_policy_document.match_trust[0].json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "match" {
  count  = var.jenkins_oidc_provider_arn == "" ? 0 : 1
  name   = "match-read-${local.service}"
  role   = aws_iam_role.match[0].id
  policy = data.aws_iam_policy_document.match.json
}

# Plain values the scripts read; listed in infra/parameters.yaml as source terraform.
data "aws_lambda_function_url" "orders_production" {
  function_name = "orders-service-production"
  qualifier     = "live"
}

resource "aws_ssm_parameter" "bucket" {
  name        = "/release-demo/${local.service}/bucket"
  type        = "String"
  value       = aws_s3_bucket.store_floor.bucket
  description = "Demo 7's bucket: fastlane match under match/, the stand-ins' state under standins/."
}

resource "aws_ssm_parameter" "orders_url" {
  name        = "/release-demo/${local.service}/orders-url"
  type        = "String"
  value       = trimsuffix(data.aws_lambda_function_url.orders_production.function_url, "/")
  description = "orders-service's production URL, which Store Floor reads today's orders from."
}

output "bucket" {
  value = aws_s3_bucket.store_floor.bucket
}
