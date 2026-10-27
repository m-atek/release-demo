# What the rollout uses: the S3 bucket Systems Manager moves Ansible's files
# through, the deploy role, and the demo's SSM parameters.

# The aws_ssm connection copies module files through S3 with presigned URLs.
# Nothing stays: objects expire after a day.
resource "aws_s3_bucket" "ssm" {
  bucket        = local.ssm_bucket
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "ssm" {
  bucket                  = aws_s3_bucket.ssm.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ssm" {
  bucket = aws_s3_bucket.ssm.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "ssm" {
  bucket = aws_s3_bucket.ssm.id
  rule {
    id     = "expire-transfers"
    status = "Enabled"
    filter {}
    expiration {
      days = 1
    }
  }
}

# The deploy role: assumed by the tools host's role in the pipeline, and by
# Mateusz's administrator sign-in for up, reset and local runs. It reaches
# only these two servers and this bucket.
data "aws_iam_policy_document" "deploy_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:PrincipalArn"
      values = [
        "arn:aws:iam::${local.account_id}:role/release-demo-tools-host",
        "arn:aws:iam::${local.account_id}:role/aws-reserved/sso.amazonaws.com/*AWSReservedSSO_AdministratorAccess_*",
      ]
    }
  }
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "FindTheServers"
    actions   = ["ec2:DescribeInstances", "ssm:DescribeInstanceInformation", "ssm:GetConnectionStatus"]
    resources = ["*"]
  }
  statement {
    sid       = "SessionsAndCommandsOnTheServers"
    actions   = ["ssm:StartSession", "ssm:SendCommand"]
    resources = [for server in aws_instance.server : server.arn]
  }
  statement {
    sid     = "TheDocumentsTheyUse"
    actions = ["ssm:StartSession", "ssm:SendCommand"]
    resources = [
      "arn:aws:ssm:${local.region}:${local.account_id}:document/SSM-SessionManagerRunShell",
      "arn:aws:ssm:${local.region}::document/AWS-RunShellScript",
      "arn:aws:ssm:${local.region}::document/AWS-StartInteractiveCommand",
    ]
  }
  statement {
    sid     = "OwnSessionsAndCommands"
    actions = ["ssm:TerminateSession", "ssm:ResumeSession"]
    # Sessions are named after the role session, which scripts/demos/linux/fleet.py names inventory-<time>.
    resources = ["arn:aws:ssm:${local.region}:${local.account_id}:session/inventory-*"]
  }
  statement {
    sid       = "CommandResults"
    actions   = ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"]
    resources = ["*"]
  }
  statement {
    sid       = "FileTransfers"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.ssm.arn}/*"]
  }
  statement {
    sid       = "FileTransferBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.ssm.arn]
  }
  # scripts/demos/linux/jcr_token.py runs on the tools host and stores the
  # read-only JCR token it makes, which the host's own role can't write.
  statement {
    sid       = "StoreTheReadToken"
    actions   = ["ssm:PutParameter"]
    resources = ["arn:aws:ssm:${local.region}:${local.account_id}:parameter/release-demo/jcr/inventory-read-token"]
  }
}

resource "aws_iam_role" "deploy" {
  name                 = "release-demo-deploy-inventory"
  assume_role_policy   = data.aws_iam_policy_document.deploy_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "deploy" {
  name   = "deploy-inventory"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}

# The app's one secret, which the release writes to the servers: from SSM on
# the cloud servers, from Jenkins credentials (the same value, through the
# tools host's tmpfs) on the data-center servers.
resource "random_password" "app_secret" {
  length  = 32
  special = false
}

resource "aws_ssm_parameter" "app_secret" {
  name        = "/release-demo/inventory/app-secret"
  type        = "SecureString"
  value       = random_password.app_secret.result
  description = substr(local.parameters["inventory/app-secret"].help, 0, 1024)
}

# Written by scripts/demos/linux/jcr_token.py; Terraform only makes the name.
resource "aws_ssm_parameter" "read_token" {
  name        = "/release-demo/jcr/inventory-read-token"
  type        = "SecureString"
  value       = "unset"
  description = substr(local.parameters["jcr/inventory-read-token"].help, 0, 1024)

  lifecycle {
    ignore_changes = [value]
  }
}

output "ssm_bucket" {
  value = aws_s3_bucket.ssm.bucket
}

output "deploy_role_arn" {
  value = aws_iam_role.deploy.arn
}
