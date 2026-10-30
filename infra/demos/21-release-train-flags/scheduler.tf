# The tools host must run at the train's departure: this schedule starts it 30
# minutes before, on weekdays, Europe/Amsterdam. It is made disabled;
# scripts/demo.sh up release-train enables it with the time from services.yaml
# (train.departures), and down disables it, so Terraform leaves both alone.

data "aws_instance" "tools_host" {
  filter {
    name   = "tag:Name"
    values = ["release-demo-tools-host"]
  }
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "release-demo-train-start-tools-host"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    actions   = ["ec2:StartInstances"]
    resources = [data.aws_instance.tools_host.arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "start-the-tools-host"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

resource "aws_scheduler_schedule" "tools_host" {
  name                         = "release-demo-train-tools-host"
  description                  = "Demo 21: starts the tools host 30 minutes before the release train's first departure, on weekdays. scripts/demo.sh up release-train enables it."
  schedule_expression          = "cron(30 13 ? * MON-FRI *)"
  schedule_expression_timezone = "Europe/Amsterdam"
  state                        = "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ec2:startInstances"
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ InstanceIds = [data.aws_instance.tools_host.id] })
  }

  lifecycle {
    ignore_changes = [state, schedule_expression]
  }
}
