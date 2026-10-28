# ORD-22: store exports. Each store's nightly export lands in this bucket, and
# S3 puts a message on the queue, so the reporting job picks it up.

resource "aws_s3_bucket" "exports" {
  bucket_prefix = "release-demo-store-exports-"
  # Exports are copies: the stores can send them again.
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "exports" {
  bucket                  = aws_s3_bucket.exports.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "exports" {
  bucket = aws_s3_bucket.exports.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "exports" {
  bucket = aws_s3_bucket.exports.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "exports" {
  bucket = aws_s3_bucket.exports.id
  rule {
    id     = "expire-old-exports"
    status = "Enabled"
    filter {}
    expiration {
      days = 30
    }
    noncurrent_version_expiration {
      noncurrent_days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_sqs_queue" "exports" {
  name                      = "release-demo-store-exports"
  message_retention_seconds = 345600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue_policy" "exports" {
  queue_url = aws_sqs_queue.exports.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "NewExportsFromTheBucket"
      Effect    = "Allow"
      Principal = { Service = "s3.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.exports.arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_s3_bucket.exports.arn } }
    }]
  })
}

resource "aws_s3_bucket_notification" "exports" {
  bucket = aws_s3_bucket.exports.id
  queue {
    queue_arn = aws_sqs_queue.exports.arn
    events    = ["s3:ObjectCreated:*"]
  }
  depends_on = [aws_sqs_queue_policy.exports]
}

output "exports_bucket" {
  value = aws_s3_bucket.exports.bucket
}

output "exports_queue_url" {
  value = aws_sqs_queue.exports.url
}
