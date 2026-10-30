# The approval bridge (bridge/handler.py): one Lambda function behind a public
# function URL. GitHub sends it the GitHub App's deployment_protection_rule
# webhook when production waits, and ServiceNow's business rule calls it when
# a change's approval is decided. It checks GitHub's signature and ServiceNow's
# token itself, so the URL needs no AWS auth. It reads only its own SSM
# parameters and invokes itself for the slow part, as the Slack handler does.
# bridge/build.sh packages it with PyJWT and cryptography for Lambda's
# platform; scripts/tf.sh runs it first.

locals {
  bridge_name = "release-demo-approval-bridge"
  bridge_arn  = "arn:aws:lambda:${local.region}:${local.account_id}:function:${local.bridge_name}"
  bridge_parameters = [
    "bridge/github-webhook-secret",
    "bridge/servicenow-token",
    "github/app-id",
    "github/app-private-key",
    "github/repository",
    "servicenow/instance-url",
    "servicenow/release-pipeline-password",
    # Demo 22: the organization's checks come only from this organization and pipeline.
    "azure-devops/organization",
    "azure-devops/pipeline-id",
  ]
}

data "archive_file" "approval_bridge" {
  type        = "zip"
  source_dir  = "${path.module}/.build/approval-bridge"
  output_path = "${path.module}/.build/approval-bridge.zip"
}

resource "aws_iam_role" "approval_bridge" {
  name               = local.bridge_name
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "approval_bridge" {
  name              = "/aws/lambda/${local.bridge_name}"
  retention_in_days = 14
}

data "aws_iam_policy_document" "approval_bridge" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.approval_bridge.arn}:*"]
  }
  statement {
    sid       = "ReadOwnParameters"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = [for name in local.bridge_parameters : "arn:aws:ssm:${local.region}:${local.account_id}:parameter/release-demo/${name}"]
  }
  statement {
    sid       = "InvokeItself"
    actions   = ["lambda:InvokeFunction"]
    resources = [local.bridge_arn, "${local.bridge_arn}:*"]
  }
  statement {
    sid       = "PendingChecks"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
    resources = [aws_dynamodb_table.bridge_checks.arn]
  }
}

resource "aws_iam_role_policy" "approval_bridge" {
  name   = "own-parameters-and-itself"
  role   = aws_iam_role.approval_bridge.id
  policy = data.aws_iam_policy_document.approval_bridge.json
}

resource "aws_lambda_function" "approval_bridge" {
  function_name = local.bridge_name
  description   = "Production waits for ServiceNow: answers the GitHub App's protection rule from the change's approval."
  role          = aws_iam_role.approval_bridge.arn
  runtime       = "python3.13"
  handler       = "handler.handler"
  # CPU scales with memory: a cold start must still answer GitHub within its 10 seconds.
  memory_size      = 1024
  timeout          = 30
  filename         = data.archive_file.approval_bridge.output_path
  source_code_hash = data.archive_file.approval_bridge.output_base64sha256

  environment {
    variables = { CHECKS_TABLE = aws_dynamodb_table.bridge_checks.name }
  }

  depends_on = [aws_cloudwatch_log_group.approval_bridge, aws_iam_role_policy.approval_bridge]
}

# The asynchronous call runs once: a retry could answer a run twice.
resource "aws_lambda_function_event_invoke_config" "approval_bridge" {
  function_name                = aws_lambda_function.approval_bridge.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 300
}

resource "aws_lambda_function_url" "approval_bridge" {
  function_name      = aws_lambda_function.approval_bridge.function_name
  authorization_type = "NONE"
}

# As for the Slack handler: lambda:InvokeFunctionUrl here, and
# aws_lambda_function_url adds the lambda:InvokeFunction statement limited to
# calls through the URL (FunctionURLAllowInvokeAction) itself.
resource "aws_lambda_permission" "approval_bridge_url" {
  statement_id           = "FunctionURLAllowPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.approval_bridge.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

# servicenow_setup.py and scripts/demos/github_actions/app_hook.py read it.
resource "aws_ssm_parameter" "bridge_url" {
  name        = "/release-demo/bridge/url"
  type        = "String"
  value       = aws_lambda_function_url.approval_bridge.function_url
  description = substr(local.parameters["bridge/url"].help, 0, 1024)
}

output "approval_bridge_url" {
  description = "Where GitHub's webhook and ServiceNow's business rule call the approval bridge: <url>github and <url>servicenow."
  value       = aws_lambda_function_url.approval_bridge.function_url
}

# Demo 22: Azure DevOps' checks that wait for ServiceNow, by the change's
# correlation id, each with its callback and the job access token Azure
# DevOps issued for it. On demand, encrypted at rest, and each item expires
# 48 hours after delivery, the token's life. Only the bridge's role may read
# or write its items.
resource "aws_dynamodb_table" "bridge_checks" {
  name         = "release-demo-approval-bridge-checks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "correlation_id"

  attribute {
    name = "correlation_id"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  server_side_encryption {
    enabled = true
  }
}

data "aws_iam_policy_document" "bridge_checks" {
  statement {
    sid    = "OnlyTheBridgeTouchesItems"
    effect = "Deny"
    actions = ["dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:PutItem",
    "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem", "dynamodb:PartiQL*"]
    resources = [aws_dynamodb_table.bridge_checks.arn]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = [aws_iam_role.approval_bridge.arn]
    }
  }
}

resource "aws_dynamodb_resource_policy" "bridge_checks" {
  resource_arn = aws_dynamodb_table.bridge_checks.arn
  policy       = data.aws_iam_policy_document.bridge_checks.json
}
