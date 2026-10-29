# The account's CloudTrail trail. EventBridge sees "AWS API Call via
# CloudTrail" events only while a trail logs management events, and the
# drift trigger (drift_trigger.tf) listens for those. So this trail is as small
# as that allows: eu-west-1 only, write management events only, no data
# events, into its own bucket that keeps a week. The first copy of management
# events is free; the bucket costs cents a month.

locals {
  trail_name = "release-demo-management-events"
  # Known before the trail exists: the bucket policy names it, and the trail
  # needs the policy first.
  trail_arn = "arn:aws:cloudtrail:eu-west-1:${local.account_id}:trail/${local.trail_name}"
}

resource "aws_s3_bucket" "trail" {
  bucket_prefix = "release-demo-trail-"
  # Copies of what CloudTrail's event history keeps for 90 days anyway.
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "trail" {
  bucket                  = aws_s3_bucket.trail.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "trail" {
  bucket = aws_s3_bucket.trail.id
  rule {
    id     = "keep-a-week"
    status = "Enabled"
    filter {}
    expiration {
      days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# Only this trail may write here, and only into this account's folder.
data "aws_iam_policy_document" "trail_bucket" {
  statement {
    sid       = "CloudTrailChecksTheBucket"
    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.trail.arn]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.trail_arn]
    }
  }
  statement {
    sid       = "CloudTrailWritesItsLogs"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.trail.arn}/AWSLogs/${local.account_id}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.trail_arn]
    }
  }
}

resource "aws_s3_bucket_policy" "trail" {
  bucket = aws_s3_bucket.trail.id
  policy = data.aws_iam_policy_document.trail_bucket.json
  # One change at a time to a new bucket: S3 can refuse two at once (OperationAborted).
  depends_on = [aws_s3_bucket_public_access_block.trail]
}

resource "aws_cloudtrail" "management" {
  name                          = local.trail_name
  s3_bucket_name                = aws_s3_bucket.trail.id
  is_multi_region_trail         = false
  include_global_service_events = false

  # Write management events only: every call the drift trigger listens for
  # changes something. Reads, such as each plan's Describe calls, stay out.
  event_selector {
    read_write_type           = "WriteOnly"
    include_management_events = true
  }

  depends_on = [aws_s3_bucket_policy.trail]
}
