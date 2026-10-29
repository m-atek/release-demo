# The drift trigger: the drift check a minute or two after someone changes the
# network stack outside Terraform, besides 07:00 on weekdays and by hand.
#
# CloudTrail (trail.tf) passes each write call to EventBridge. This rule picks
# the calls that change a security group's rules, a bucket's policy or its
# public access block, and hands them to the Lambda function
# release-demo-drift-trigger (scripts/demos/terraform/drift_trigger.py). For a
# change to the stack that its own Terraform roles didn't make, the function
# starts the Jenkins job network-drift as release-bot through Cloudflare
# Access, as the Slack handler does, with the Slack handler's service token,
# which it reads from SSM when it runs. Cloudflare stays as it is.

locals {
  drift_trigger_name = "release-demo-drift-trigger"
  drift_trigger_parameters = [
    "cloudflare/slack-handler-client-id",
    "cloudflare/slack-handler-client-secret",
    "jenkins/release-bot-api-token",
  ]
  group_calls = [
    "AuthorizeSecurityGroupIngress", "AuthorizeSecurityGroupEgress",
    "RevokeSecurityGroupIngress", "RevokeSecurityGroupEgress",
    "ModifySecurityGroupRules",
  ]
  bucket_calls = [
    "PutBucketPolicy", "DeleteBucketPolicy",
    "PutBucketPublicAccessBlock", "DeleteBucketPublicAccessBlock",
  ]
}

resource "aws_cloudwatch_event_rule" "drift_trigger" {
  name        = "release-demo-network-stack-changes"
  description = "Demo 2: security group rule, bucket policy and public access block changes, for the drift check."
  event_pattern = jsonencode({
    source        = ["aws.ec2", "aws.s3"]
    "detail-type" = ["AWS API Call via CloudTrail"]
    detail = {
      eventSource = ["ec2.amazonaws.com", "s3.amazonaws.com"]
      eventName   = concat(local.group_calls, local.bucket_calls)
      # A call that failed changed nothing.
      errorCode = [{ exists = false }]
    }
  })
}

resource "aws_cloudwatch_event_target" "drift_trigger" {
  rule = aws_cloudwatch_event_rule.drift_trigger.name
  arn  = aws_lambda_function.drift_trigger.arn
}

resource "aws_lambda_permission" "drift_trigger" {
  statement_id  = "FromTheNetworkStackChangesRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.drift_trigger.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.drift_trigger.arn
}

data "archive_file" "drift_trigger" {
  type        = "zip"
  output_path = "${path.module}/.build/drift-trigger.zip"

  source {
    filename = "drift_trigger.py"
    content  = file("${path.module}/../../../scripts/demos/terraform/drift_trigger.py")
  }
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "drift_trigger" {
  name               = local.drift_trigger_name
  description        = "Demo 2: starts the drift check on Jenkins after a change made outside Terraform."
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "drift_trigger" {
  name              = "/aws/lambda/${local.drift_trigger_name}"
  retention_in_days = 14
}

data "aws_iam_policy_document" "drift_trigger" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.drift_trigger.arn}:*"]
  }
  statement {
    sid       = "ReadReleaseBotAndAccessToken"
    actions   = ["ssm:GetParameters"]
    resources = [for name in local.drift_trigger_parameters : "arn:aws:ssm:eu-west-1:${local.account_id}:parameter/release-demo/${name}"]
  }
  # Neither call has resource-level permissions: each is all or nothing within
  # the region. The function asks for the tools host and the changed group only.
  statement {
    sid       = "DescribeTheToolsHostAndGroups"
    actions   = ["ec2:DescribeInstances", "ec2:DescribeSecurityGroups"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = ["eu-west-1"]
    }
  }
}

resource "aws_iam_role_policy" "drift_trigger" {
  name   = "logs-jenkins-token-and-describe"
  role   = aws_iam_role.drift_trigger.id
  policy = data.aws_iam_policy_document.drift_trigger.json
}

resource "aws_lambda_function" "drift_trigger" {
  function_name    = local.drift_trigger_name
  description      = "Demo 2: starts network-drift on Jenkins after a change made outside Terraform."
  role             = aws_iam_role.drift_trigger.arn
  runtime          = "python3.13"
  handler          = "drift_trigger.handler"
  memory_size      = 256
  timeout          = 30
  filename         = data.archive_file.drift_trigger.output_path
  source_code_hash = data.archive_file.drift_trigger.output_base64sha256

  environment {
    variables = {
      JENKINS_URL = "https://jenkins-demo.es.nl"
      # The pull requests, reset.py terraform and stack.py up apply as the apply role.
      IGNORED_ROLES = join(",", [aws_iam_role.plan.name, aws_iam_role.apply.name])
      DEMO_TAG      = local.demo_tag
      BUCKET_PREFIX = local.exports_bucket_prefix
    }
  }

  depends_on = [aws_cloudwatch_log_group.drift_trigger, aws_iam_role_policy.drift_trigger]
}

# The function never raises, so Lambda has nothing to retry. A change that
# waits more than 15 minutes for one of the account's 10 concurrent executions
# is dropped: the 07:00 run finds it.
resource "aws_lambda_function_event_invoke_config" "drift_trigger" {
  function_name                = aws_lambda_function.drift_trigger.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 900
}
