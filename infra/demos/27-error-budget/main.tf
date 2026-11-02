# Video 27, the error budget: the load generator, one request a second to
# orders-service production, so CloudWatch has a filled 24 hours to count. Its
# own root and state; run it through scripts/demos/error_budget/tf.sh, which
# signs in with your short-lived credentials and points the backend at the state
# bucket of infra/. It holds IAM, so you apply it.
#
# The rule fires every minute, always. The function reads
# error-budget/traffic-until first and exits at once when it's off or past, so
# an idle day costs a fraction of a cent. scripts/demo.sh up error-budget sets
# it to now + 72 hours; down sets it off.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.69.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.8.0" }
  }
  # The bucket comes from SSM at init: scripts/demos/error_budget/tf.sh init.
  backend "s3" {
    key          = "release-demo/demos/27-error-budget.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "27-error-budget", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  name       = "release-demo-error-budget-traffic"
  budget     = yamldecode(file("${path.module}/../../../services.yaml"))["orders-service"].error_budget
  parameter  = "arn:aws:ssm:${local.region}:${local.account_id}:parameter/release-demo"
}

# The production function URL of the alias live: whichever version is live answers.
data "aws_lambda_function_url" "production" {
  function_name = local.budget.function
  qualifier     = "live"
}

data "archive_file" "traffic" {
  type        = "zip"
  output_path = "${path.module}/.build/traffic.zip"

  source {
    filename = "traffic.py"
    content  = file("${path.module}/../../../scripts/demos/error_budget/traffic.py")
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

resource "aws_iam_role" "traffic" {
  name               = local.name
  description        = "Video 27: sends one request a second to orders-service production while error-budget/traffic-until says so."
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "traffic" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = 14
}

# Logs to its own group, the switch and Grafana's log push, and KMS only through SSM.
data "aws_iam_policy_document" "traffic" {
  statement {
    sid       = "OwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.traffic.arn}:*"]
  }
  statement {
    sid     = "TheSwitchAndGrafanasLogPush"
    actions = ["ssm:GetParameter"]
    resources = [for name in ["error-budget/traffic-until", "grafana/logs-url", "grafana/logs-user", "grafana/logs-token"] :
    "${local.parameter}/${name}"]
  }
  statement {
    sid       = "DecryptThroughSsm"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${local.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "traffic" {
  name   = "logs-switch-and-log-push"
  role   = aws_iam_role.traffic.id
  policy = data.aws_iam_policy_document.traffic.json
}

resource "aws_lambda_function" "traffic" {
  function_name    = local.name
  description      = "Video 27: the load generator behind the error budget, 60 requests a minute while its switch is on."
  role             = aws_iam_role.traffic.arn
  runtime          = "python3.13"
  handler          = "traffic.handler"
  memory_size      = 128
  timeout          = 65
  filename         = data.archive_file.traffic.output_path
  source_code_hash = data.archive_file.traffic.output_base64sha256

  environment {
    variables = { TARGET_URL = data.aws_lambda_function_url.production.function_url }
  }

  depends_on = [aws_cloudwatch_log_group.traffic, aws_iam_role_policy.traffic]
}

# A run that fails isn't run again: a retry would send a second minute's requests on top of the next run's.
resource "aws_lambda_function_event_invoke_config" "traffic" {
  function_name                = aws_lambda_function.traffic.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 60
}

resource "aws_cloudwatch_event_rule" "traffic" {
  name                = local.name
  description         = "Video 27: starts the load generator every minute. It exits at once while its switch is off."
  schedule_expression = "rate(1 minute)"
}

resource "aws_cloudwatch_event_target" "traffic" {
  rule = aws_cloudwatch_event_rule.traffic.name
  arn  = aws_lambda_function.traffic.arn

  retry_policy {
    maximum_retry_attempts       = 0
    maximum_event_age_in_seconds = 60
  }
}

resource "aws_lambda_permission" "traffic" {
  statement_id  = "AllowTheRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.traffic.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.traffic.arn
}

# reset.py error-budget and the main reset run on the tools host, and they turn
# the check's switch on and off. reset.py error-budget also writes the seeded
# fill from there (scripts/demos/error_budget/seed.py). Here and not in
# infra/tools_host.tf, so the main root's apply for this demo stays free of IAM.
data "aws_iam_policy_document" "tools_host_switch" {
  statement {
    sid       = "WriteTheBudgetsSwitch"
    actions   = ["ssm:PutParameter"]
    resources = ["${local.parameter}/error-budget/enabled"]
  }
  # This call has no resource-level permissions: the namespace is what narrows it.
  statement {
    sid       = "WriteTheSeededFill"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = [try(local.budget.seed.namespace, "ReleaseDemo/ErrorBudget")]
    }
  }
}

resource "aws_iam_role_policy" "tools_host_switch" {
  name   = "error-budget-switch-and-seed"
  role   = "release-demo-tools-host"
  policy = data.aws_iam_policy_document.tools_host_switch.json
}

output "traffic_function" {
  description = "The load generator."
  value       = aws_lambda_function.traffic.function_name
}
