# The Jenkins identity provider. Jenkins's OpenID Connect Provider plugin signs
# an ID token per build, but Jenkins sits behind Cloudflare Access, so the
# clouds read its public keys from an alternate issuer: this bucket, which
# holds two files and nothing else. scripts/demos/clouds/oidc_issuer.py
# publishes them again whenever Jenkins makes new keys (every start).
#
# It is the account's only public bucket, on purpose, and tagged as the
# exception. It lives here, outside the stack demo 2's house rules check.

resource "random_id" "issuer" {
  byte_length = 4
}

resource "aws_s3_bucket" "issuer" {
  bucket = "release-demo-oidc-${random_id.issuer.hex}"
  tags = {
    # S3 tag values allow no commas.
    Exception = "public-read: only the two OpenID Connect discovery files of Jenkins"
  }
}

resource "aws_s3_bucket_ownership_controls" "issuer" {
  bucket = aws_s3_bucket.issuer.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Public through the bucket policy below only: no ACLs.
resource "aws_s3_bucket_public_access_block" "issuer" {
  bucket                  = aws_s3_bucket.issuer.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = false
  restrict_public_buckets = false
}

locals {
  issuer      = "https://${aws_s3_bucket.issuer.bucket_regional_domain_name}"
  issuer_host = aws_s3_bucket.issuer.bucket_regional_domain_name
  issuer_files = [
    "${aws_s3_bucket.issuer.arn}/.well-known/openid-configuration",
    "${aws_s3_bucket.issuer.arn}/jwks",
  ]
}

data "aws_iam_policy_document" "issuer_public_read" {
  statement {
    sid       = "AnyoneMayReadTheTwoDiscoveryFiles"
    actions   = ["s3:GetObject"]
    resources = local.issuer_files
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
}

resource "aws_s3_bucket_policy" "issuer" {
  bucket     = aws_s3_bucket.issuer.id
  policy     = data.aws_iam_policy_document.issuer_public_read.json
  depends_on = [aws_s3_bucket_public_access_block.issuer]
}

# The tools host publishes the files at boot through this role, which may
# write those two files and nothing else.
data "aws_iam_policy_document" "issuer_publisher_trust" {
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

data "aws_iam_policy_document" "issuer_publisher" {
  statement {
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = local.issuer_files
  }
}

resource "aws_iam_role" "issuer_publisher" {
  name                 = "release-demo-deploy-oidc-issuer"
  assume_role_policy   = data.aws_iam_policy_document.issuer_publisher_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "issuer_publisher" {
  name   = "publish-oidc-discovery-files"
  role   = aws_iam_role.issuer_publisher.id
  policy = data.aws_iam_policy_document.issuer_publisher.json
}

# The files as the plugin writes them, so the issuer is valid from the start:
# AWS reads the discovery document when it creates the provider below. The
# key set starts empty; oidc_issuer.py replaces both with Jenkins's own.
resource "aws_s3_object" "discovery" {
  bucket       = aws_s3_bucket.issuer.id
  key          = ".well-known/openid-configuration"
  content_type = "application/json"
  content = jsonencode({
    issuer                                = local.issuer
    jwks_uri                              = "${local.issuer}/jwks"
    response_types_supported              = ["code"]
    subject_types_supported               = ["public"]
    id_token_signing_alg_values_supported = ["RS256"]
    authorization_endpoint                = "https://unimplemented"
    token_endpoint                        = "https://unimplemented"
  })
  lifecycle {
    ignore_changes = [content, etag, cache_control]
  }
}

resource "aws_s3_object" "jwks" {
  bucket       = aws_s3_bucket.issuer.id
  key          = "jwks"
  content_type = "application/json"
  content      = jsonencode({ keys = [] })
  lifecycle {
    ignore_changes = [content, etag, cache_control]
  }
}

# AWS trusts the issuer. Which roles a token may assume is up to each role's
# trust policy: quote-service's is in aws.tf.
resource "aws_iam_openid_connect_provider" "jenkins" {
  url            = local.issuer
  client_id_list = [local.aws_audience]
  depends_on     = [aws_s3_bucket_policy.issuer, aws_s3_object.discovery, aws_s3_object.jwks]
}

# casc.yaml names the issuer in each ID token credential, through the host's
# secret files: ${jenkins-oidc-issuer}.
resource "aws_ssm_parameter" "issuer" {
  name        = "/release-demo/jenkins/oidc-issuer"
  type        = "String"
  value       = local.issuer
  description = "The issuer of Jenkins's ID tokens: a public S3 bucket with the discovery files. Terraform, infra/demos/5-three-clouds."
}

output "oidc_issuer" {
  description = "The issuer every cloud trusts."
  value       = local.issuer
}
