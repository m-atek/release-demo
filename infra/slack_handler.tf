# The Slack handler: one Lambda function behind a public function URL that
# Slack's /release and /rollback post to. It checks Slack's signature itself,
# so the URL needs no AWS auth. It reads only its own SSM parameters,
# describes the tools host, and invokes itself for the slow part. The code is
# slack/handler.py; config.json carries what it needs from services.yaml.

locals {
  people = yamldecode(file("${path.module}/../people.yaml"))
  # Only these people may start releases and rollbacks. Their Slack member ids
  # are in SSM, slack/member-id/<name>, like common.people() reads them.
  releasers = sort([for name, person in local.people : name if try(person.releaser, false)])

  slack_handler_name = "release-demo-slack-handler"
  slack_handler_arn  = "arn:aws:lambda:${local.region}:${local.account_id}:function:${local.slack_handler_name}"
  slack_handler_parameters = concat(
    [
      "slack/signing-secret",
      "slack/bot-token",
      "cloudflare/slack-handler-client-id",
      "cloudflare/slack-handler-client-secret",
      "jenkins/release-bot-api-token",
    ],
    [for name in local.releasers : "slack/member-id/${name}"],
    # Demo 17: which CI /release starts (ci/<service>), and GitLab's trigger.
    ["ci/*", "gitlab/project-id", "gitlab/trigger-token"],
  )
  slack_handler_config = {
    services = {
      # A service released through pull requests (demo 2) has no /release.
      for name, s in local.services : name => {
        jira_project = s.jira.project
        options      = try(s.slack.options, [])
      } if try(s.pipeline, "release") != "pull-request"
    }
  }
}

data "archive_file" "slack_handler" {
  type        = "zip"
  output_path = "${path.module}/.build/slack-handler.zip"

  source {
    filename = "handler.py"
    content  = file("${path.module}/../slack/handler.py")
  }
  source {
    filename = "config.json"
    content  = jsonencode(local.slack_handler_config)
  }
}

resource "aws_iam_role" "slack_handler" {
  name               = local.slack_handler_name
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "slack_handler" {
  name              = "/aws/lambda/${local.slack_handler_name}"
  retention_in_days = 14
}

data "aws_iam_policy_document" "slack_handler" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.slack_handler.arn}:*"]
  }
  statement {
    sid       = "ReadOwnParameters"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = [for name in local.slack_handler_parameters : "arn:aws:ssm:${local.region}:${local.account_id}:parameter/release-demo/${name}"]
  }
  # DescribeInstances has no resource-level permissions: it is all or nothing
  # within the region. The handler asks for the tools host only.
  statement {
    sid       = "DescribeTheToolsHost"
    actions   = ["ec2:DescribeInstances"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.region]
    }
  }
  statement {
    sid       = "InvokeItself"
    actions   = ["lambda:InvokeFunction"]
    resources = [local.slack_handler_arn, "${local.slack_handler_arn}:*"]
  }
}

resource "aws_iam_role_policy" "slack_handler" {
  name   = "own-parameters-tools-host-and-itself"
  role   = aws_iam_role.slack_handler.id
  policy = data.aws_iam_policy_document.slack_handler.json
}

resource "aws_lambda_function" "slack_handler" {
  function_name = local.slack_handler_name
  description   = "Slack /release and /rollback to Jenkins. Demo system, not a client's."
  role          = aws_iam_role.slack_handler.arn
  runtime       = "python3.13"
  handler       = "handler.handler"
  # CPU scales with memory: a cold start must still answer Slack within 3 seconds.
  memory_size      = 1024
  timeout          = 30
  filename         = data.archive_file.slack_handler.output_path
  source_code_hash = data.archive_file.slack_handler.output_base64sha256

  environment {
    variables = {
      RELEASERS     = join(",", local.releasers)
      TOOLS_HOST_ID = aws_instance.tools_host.id
      JENKINS_URL   = "https://${local.tool_hostnames.jenkins.hostname}"
    }
  }

  depends_on = [aws_cloudwatch_log_group.slack_handler, aws_iam_role_policy.slack_handler]
}

# The asynchronous call runs once: a retry could post a second message and
# start a second run. A request that waited a minute is dropped.
resource "aws_lambda_function_event_invoke_config" "slack_handler" {
  function_name                = aws_lambda_function.slack_handler.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 60
}

resource "aws_lambda_function_url" "slack_handler" {
  function_name      = aws_lambda_function.slack_handler.function_name
  authorization_type = "NONE"
}

# As for orders-service: lambda:InvokeFunctionUrl here, and
# aws_lambda_function_url adds the lambda:InvokeFunction statement limited to
# calls through the URL (FunctionURLAllowInvokeAction) itself.
resource "aws_lambda_permission" "slack_handler_url" {
  statement_id           = "FunctionURLAllowPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.slack_handler.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

output "slack_handler_url" {
  description = "The request URL of /release and /rollback: scripts/slack_manifest.py puts it into the Slack app manifest."
  value       = aws_lambda_function_url.slack_handler.function_url
}
