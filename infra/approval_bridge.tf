# The bridge: one Lambda function behind a public function URL, for calls from
# outside the tools host that change the platform's records. Its code is
# bridge/; each route checks its own caller, so the URL needs no AWS auth.
#
# Built here for demo 19's event bridge, POST /events, which the drift
# recorder on the cluster host calls with a bearer token. Demo 14 adds its
# approval routes and demo 21 AWS AppConfig's events to the same function.
# It reads only the parameters it writes with, and never the tools host's.

locals {
  bridge_name = "release-demo-bridge"
  bridge_arn  = "arn:aws:lambda:${local.region}:${local.account_id}:function:${local.bridge_name}"
  bridge_parameters = [
    "bridge/events-token",
    "servicenow/instance-url",
    "servicenow/release-pipeline-password",
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
  output_path = "${path.module}/.build/bridge.zip"
  source_dir  = "${path.module}/../bridge"
  excludes    = ["tests", "__pycache__", "tests/__pycache__"]
}

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
}

resource "aws_iam_role_policy" "bridge" {
  name   = "own-parameters"
  role   = aws_iam_role.bridge.id
  policy = data.aws_iam_policy_document.bridge.json
}

resource "aws_lambda_function" "bridge" {
  function_name    = local.bridge_name
  description      = "Events from the platform's machines into ServiceNow, Slack and Grafana."
  role             = aws_iam_role.bridge.arn
  runtime          = "python3.13"
  handler          = "handler.handler"
  memory_size      = 512
  timeout          = 60
  filename         = data.archive_file.bridge.output_path
  source_code_hash = data.archive_file.bridge.output_base64sha256

  depends_on = [aws_cloudwatch_log_group.bridge, aws_iam_role_policy.bridge]
}

resource "aws_lambda_function_url" "bridge" {
  function_name      = aws_lambda_function.bridge.function_name
  authorization_type = "NONE"
}

# As for the Slack handler: lambda:InvokeFunctionUrl here, and
# aws_lambda_function_url adds the lambda:InvokeFunction statement itself.
resource "aws_lambda_permission" "bridge_url" {
  statement_id           = "FunctionURLAllowPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.bridge.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_ssm_parameter" "bridge_url" {
  name        = "/release-demo/bridge/url"
  type        = "String"
  value       = aws_lambda_function_url.bridge.function_url
  description = substr(local.parameters["bridge/url"].help, 0, 1024)
}
