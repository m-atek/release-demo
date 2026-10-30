# The event bridge's AWS AppConfig route: AppConfig's deployment events reach
# Amazon EventBridge through AWS's own extension, and a rule passes
# production's to bridge/appconfig.py, which records each switch as a change,
# in Slack and in Grafana. It reads only the parameters it writes with, and
# only this application's deployments and versions.

resource "aws_appconfig_extension_association" "events" {
  extension_arn = "arn:aws:appconfig:${local.region}::extension/AWS.AppConfig.DeploymentNotificationsToEventBridge/1"
  resource_arn  = aws_appconfig_application.orders.arn
}

locals {
  bridge_parameters = ["slack/bot-token", "slack/channel-id", "servicenow/instance-url",
  "servicenow/release-pipeline-password", "grafana/stack-url", "grafana/service-account-token"]
  bridge_config = {
    service       = "orders-service"
    environment   = "production"
    template      = local.orders.servicenow.templates.flag
    alarm         = local.alarm_name
    watch_minutes = local.watch_minutes
    # The people /flag names in a deployment's description, and their ServiceNow users.
    people = { for name, person in local.people : person.display_name => { servicenow_user = person.servicenow_user }
    if try(person.releaser, false) }
  }
}

data "archive_file" "bridge" {
  type        = "zip"
  output_path = "${path.module}/.terraform/build/appconfig-events.zip"

  source {
    filename = "appconfig.py"
    content  = file("${path.module}/../../../bridge/appconfig.py")
  }
  source {
    filename = "config.json"
    content  = jsonencode(local.bridge_config)
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
    sid       = "ReadWhatItWritesWith"
    actions   = ["ssm:GetParameters"]
    resources = [for name in local.bridge_parameters : "arn:aws:ssm:${local.region}:${local.account_id}:parameter/release-demo/${name}"]
  }
  statement {
    sid       = "ReadTheDeploymentAndKeepItsRecordOnIt"
    actions   = ["appconfig:GetDeployment", "appconfig:ListTagsForResource", "appconfig:TagResource"]
    resources = ["${local.environment_arns.production}/deployment/*"]
  }
  statement {
    sid       = "ReadTheFlagsItDeployed"
    actions   = ["appconfig:GetHostedConfigurationVersion"]
    resources = ["${local.profile_arn}/hostedconfigurationversion/*"]
  }
  statement {
    sid       = "WhenTheAlarmFired"
    actions   = ["cloudwatch:DescribeAlarmHistory"]
    resources = [aws_cloudwatch_metric_alarm.production_errors.arn]
  }
}

resource "aws_iam_role_policy" "bridge" {
  name   = "appconfig-events-to-records"
  role   = aws_iam_role.bridge.id
  policy = data.aws_iam_policy_document.bridge.json
}

resource "aws_lambda_function" "bridge" {
  function_name    = local.bridge_name
  description      = "Demo 21: AWS AppConfig's flag deployments in orders-service production, recorded as changes, in Slack and in Grafana."
  role             = aws_iam_role.bridge.arn
  runtime          = "python3.13"
  handler          = "appconfig.handler"
  memory_size      = 256
  timeout          = 60
  filename         = data.archive_file.bridge.output_path
  source_code_hash = data.archive_file.bridge.output_base64sha256

  depends_on = [aws_cloudwatch_log_group.bridge, aws_iam_role_policy.bridge]
}

# EventBridge retries a failed call; the deployment's tags keep a retry from recording twice.
resource "aws_lambda_function_event_invoke_config" "bridge" {
  function_name                = aws_lambda_function.bridge.function_name
  maximum_retry_attempts       = 2
  maximum_event_age_in_seconds = 3600
}

resource "aws_cloudwatch_event_rule" "appconfig" {
  name        = "release-demo-appconfig-orders-service"
  description = "Demo 21: AWS AppConfig's deployments of orders-service's flags in production, to the event bridge."
  event_pattern = jsonencode({
    source = ["aws.appconfig"]
    detail = {
      Type        = ["OnDeploymentStart", "OnDeploymentComplete", "OnDeploymentRolledBack"]
      Application = { Name = [aws_appconfig_application.orders.name] }
      Environment = { Name = ["production"] }
    }
  })
}

resource "aws_cloudwatch_event_target" "bridge" {
  rule = aws_cloudwatch_event_rule.appconfig.name
  arn  = aws_lambda_function.bridge.arn
}

resource "aws_lambda_permission" "events" {
  statement_id  = "AllowAppConfigEventsFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.bridge.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.appconfig.arn
}
