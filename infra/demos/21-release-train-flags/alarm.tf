# The alarm AWS AppConfig watches after a flag switch in production: the
# function's errors through the alias live, which the store apps and the
# traffic call. At 5 or more in a minute it goes into ALARM, and AppConfig
# rolls the switch back. About USD 0.10 a month.

resource "aws_cloudwatch_metric_alarm" "production_errors" {
  alarm_name          = local.alarm_name
  alarm_description   = "orders-service production's errors through the alias live, per minute. AWS AppConfig watches it for ${local.watch_minutes} minutes after each flag switch, and rolls the switch back when it fires."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = local.orders.environments.production.function, Resource = "${local.orders.environments.production.function}:live" }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
}

# The role AWS AppConfig reads the alarm with: DescribeAlarms only, as AppConfig documents it.
data "aws_iam_policy_document" "appconfig_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["appconfig.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "appconfig_monitor" {
  name               = "release-demo-appconfig-alarm"
  assume_role_policy = data.aws_iam_policy_document.appconfig_assume.json
}

data "aws_iam_policy_document" "appconfig_monitor" {
  statement {
    actions   = ["cloudwatch:DescribeAlarms"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "appconfig_monitor" {
  name   = "describe-alarms"
  role   = aws_iam_role.appconfig_monitor.id
  policy = data.aws_iam_policy_document.appconfig_monitor.json
}
