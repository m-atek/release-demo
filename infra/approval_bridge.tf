# The bridge (bridge/), release-demo-bridge: one Lambda function behind a
# public function URL, for calls from outside the tools host that wait on, or
# change, the platform's records. Each route checks its own caller, so the URL
# needs no AWS auth:
#
# - the approval bridge (demos 14 and 22): GitHub sends the GitHub App's
#   deployment_protection_rule webhook to /github when production waits,
#   Azure DevOps its Invoke REST API check to /azure-devops/check, and
#   ServiceNow's business rule calls /servicenow when a change's approval is
#   decided;
# - the event bridge (demo 19): POST /events, with a bearer token, from the
#   platform's own machines, such as the drift recorder on the cluster host,
#   recorded in ServiceNow, Slack and Grafana (bridge/events.py).
#
# It reads only its own SSM parameters, never the tools host's, and invokes
# itself for the slow part, as the Slack handler does. bridge/build.sh
# packages it with PyJWT and cryptography for Lambda's platform; scripts/tf.sh
# runs it first. AWS AppConfig's events (demo 21) have a function of their
# own, in infra/demos/21-release-train-flags.

locals {
  bridge_name = "release-demo-bridge"
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
    # Demo 19: the events route's token, and where it records an event.
    "bridge/events-token",
    "slack/bot-token",
    "slack/channel-id",
    "grafana/stack-url",
    "grafana/service-account-token",
    "grafana/logs-url",
    "grafana/logs-user",
    "grafana/logs-token",
  ]
}

data "archive_file" "bridge" {
  type        = "zip"
  source_dir  = "${path.module}/.build/bridge"
  output_path = "${path.module}/.build/bridge.zip"
}

# The drift recorder's bearer token for POST /events, generated here like the
# platform's other machine tokens.
resource "random_password" "bridge_events_token" {
  length  = 40
  special = false
}

resource "aws_ssm_parameter" "bridge_events_token" {
  name        = "/release-demo/bridge/events-token"
  type        = "SecureString"
  value       = random_password.bridge_events_token.result
  description = substr(local.parameters["bridge/events-token"].help, 0, 1024)
}

resource "aws_iam_role" "bridge" {
  name               = local.bridge_name
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "bridge" {
  name              = "/aws/lambda/${local.bridge_name}"
  retention_in_days = 14
}

data "aws_iam_policy_document" "bridge" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.bridge.arn}:*"]
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

resource "aws_iam_role_policy" "bridge" {
  name   = "own-parameters-and-itself"
  role   = aws_iam_role.bridge.id
  policy = data.aws_iam_policy_document.bridge.json
}

resource "aws_lambda_function" "bridge" {
  function_name = local.bridge_name
  description   = "Production waits for ServiceNow (GitHub Actions, Azure Pipelines); the platform's events into ServiceNow, Slack and Grafana."
  role          = aws_iam_role.bridge.arn
  runtime       = "python3.13"
  handler       = "handler.handler"
  # CPU scales with memory: a cold start must still answer GitHub within its 10 seconds.
  memory_size = 1024
  # An event's incident, Slack message and annotation, or a sleeping ServiceNow, take their time.
  timeout          = 60
  filename         = data.archive_file.bridge.output_path
  source_code_hash = data.archive_file.bridge.output_base64sha256

  environment {
    variables = { CHECKS_TABLE = aws_dynamodb_table.bridge_checks.name }
  }

  depends_on = [aws_cloudwatch_log_group.bridge, aws_iam_role_policy.bridge]
}

# The asynchronous call runs once: a retry could answer a run twice.
resource "aws_lambda_function_event_invoke_config" "bridge" {
  function_name                = aws_lambda_function.bridge.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 300
}

resource "aws_lambda_function_url" "bridge" {
  function_name      = aws_lambda_function.bridge.function_name
  authorization_type = "NONE"
}

# As for the Slack handler: lambda:InvokeFunctionUrl here, and
# aws_lambda_function_url adds the lambda:InvokeFunction statement limited to
# calls through the URL (FunctionURLAllowInvokeAction) itself.
resource "aws_lambda_permission" "bridge_url" {
  statement_id           = "FunctionURLAllowPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.bridge.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

# servicenow_setup.py, scripts/demos/github_actions/app_hook.py and demo 19's
# cluster host read it.
resource "aws_ssm_parameter" "bridge_url" {
  name        = "/release-demo/bridge/url"
  type        = "String"
  value       = aws_lambda_function_url.bridge.function_url
  description = substr(local.parameters["bridge/url"].help, 0, 1024)
}

output "bridge_url" {
  description = "Where the bridge answers: <url>github and <url>servicenow (demo 14), <url>azure-devops/check (demo 22), <url>events (demo 19)."
  value       = aws_lambda_function_url.bridge.function_url
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
      values   = [aws_iam_role.bridge.arn]
    }
  }
}

resource "aws_dynamodb_resource_policy" "bridge_checks" {
  resource_arn = aws_dynamodb_table.bridge_checks.arn
  policy       = data.aws_iam_policy_document.bridge_checks.json
}
