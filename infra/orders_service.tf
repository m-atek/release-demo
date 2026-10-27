# orders-service: one Lambda function per environment, published versions,
# the alias live, and a public function URL on that alias. Terraform ships
# 1.1.0 once; after that the pipeline deploys code and moves the alias, so
# Terraform ignores both. Settings come from services.yaml, drift included.

locals {
  orders          = local.services["orders-service"]
  initial_version = "1.1.0"
  initial_source  = "${path.module}/../demo-states/orders-service/${local.initial_version}"
}

data "archive_file" "orders_initial" {
  type        = "zip"
  output_path = "${path.module}/.build/orders-service-${local.initial_version}.zip"

  source {
    filename = "handler.py"
    content  = file("${local.initial_source}/handler.py")
  }
  source {
    filename = "VERSION"
    content  = file("${local.initial_source}/VERSION")
  }
  source {
    filename = "build_info.json"
    content  = jsonencode({ service = "orders-service", version = local.initial_version, commit = "initial" })
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

resource "aws_iam_role" "orders" {
  for_each           = local.orders.environments
  name               = "${each.value.function}-function"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "orders" {
  for_each          = local.orders.environments
  name              = "/aws/lambda/${each.value.function}"
  retention_in_days = 14
}

# Logs, and reading its own configuration: the page shows when the running
# version was published.
data "aws_iam_policy_document" "orders" {
  for_each = local.orders.environments

  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.orders[each.key].arn}:*"]
  }
  statement {
    actions = ["lambda:GetFunctionConfiguration"]
    resources = [
      "arn:aws:lambda:${local.region}:${local.account_id}:function:${each.value.function}",
      "arn:aws:lambda:${local.region}:${local.account_id}:function:${each.value.function}:*",
    ]
  }
}

resource "aws_iam_role_policy" "orders" {
  for_each = local.orders.environments
  name     = "logs-and-own-configuration"
  role     = aws_iam_role.orders[each.key].id
  policy   = data.aws_iam_policy_document.orders[each.key].json
}

resource "aws_lambda_function" "orders" {
  for_each         = local.orders.environments
  function_name    = each.value.function
  description      = "orders-service in ${each.key}. Demo system, not a client's."
  role             = aws_iam_role.orders[each.key].arn
  runtime          = local.orders.runtime
  handler          = local.orders.handler
  # CPU scales with memory: at 128 MB the page's first request, which imports
  # boto3 to read the deploy time, took over 6 seconds.
  memory_size      = 1024
  timeout          = 10
  filename         = data.archive_file.orders_initial.output_path
  source_code_hash = data.archive_file.orders_initial.output_base64sha256
  publish          = true

  environment {
    variables = merge({ ENVIRONMENT = each.key }, each.value.settings)
  }

  # The pipeline deploys new code from JFrog Container Registry.
  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }

  depends_on = [aws_cloudwatch_log_group.orders, aws_iam_role_policy.orders]
}

resource "aws_lambda_alias" "live" {
  for_each         = local.orders.environments
  name             = "live"
  function_name    = aws_lambda_function.orders[each.key].function_name
  function_version = aws_lambda_function.orders[each.key].version

  # The pipeline, rollbacks and reset.py move the alias.
  lifecycle {
    ignore_changes = [function_version]
  }
}

resource "aws_lambda_function_url" "orders" {
  for_each           = local.orders.environments
  function_name      = aws_lambda_function.orders[each.key].function_name
  qualifier          = aws_lambda_alias.live[each.key].name
  authorization_type = "NONE"
}

# Since October 2025 a public function URL needs two statements:
# lambda:InvokeFunctionUrl, below, and lambda:InvokeFunction limited to calls
# through the URL. aws_lambda_function_url adds that second one itself
# (FunctionURLAllowInvokeAction), so it isn't declared here.
resource "aws_lambda_permission" "orders_url" {
  for_each               = local.orders.environments
  statement_id           = "FunctionURLAllowPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.orders[each.key].function_name
  qualifier              = aws_lambda_alias.live[each.key].name
  principal              = "*"
  function_url_auth_type = "NONE"
}

# The deploy role: assumed by the tools host's role only, and able to change
# nothing but these two functions and their aliases.
data "aws_iam_policy_document" "orders_deploy_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = ["arn:aws:iam::${local.account_id}:role/release-demo-tools-host"]
    }
  }
}

data "aws_iam_policy_document" "orders_deploy" {
  statement {
    actions = [
      "lambda:GetFunction",
      "lambda:GetFunctionConfiguration",
      "lambda:GetFunctionUrlConfig",
      "lambda:UpdateFunctionCode",
      "lambda:PublishVersion",
      "lambda:ListVersionsByFunction",
      "lambda:GetAlias",
      "lambda:UpdateAlias",
    ]
    resources = flatten([
      for f in aws_lambda_function.orders : [f.arn, "${f.arn}:*"]
    ])
  }
}

resource "aws_iam_role" "orders_deploy" {
  name                 = "release-demo-deploy-orders-service"
  assume_role_policy   = data.aws_iam_policy_document.orders_deploy_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "orders_deploy" {
  name   = "deploy-orders-service"
  role   = aws_iam_role.orders_deploy.id
  policy = data.aws_iam_policy_document.orders_deploy.json
}

output "orders_service_urls" {
  description = "The function URL of each environment's live alias."
  value       = { for env, url in aws_lambda_function_url.orders : env => url.function_url }
}
