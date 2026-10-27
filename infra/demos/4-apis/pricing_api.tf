# pricing-api: one Lambda function per environment, published versions, the
# alias live, and an API Gateway HTTP API in front that calls that alias.
# Terraform ships 2.0.4 once; after that the pipeline deploys code, moves the
# alias and sets its canary weights, so Terraform ignores all three.

locals {
  initial_version = "2.0.4"
  initial_source  = "${path.module}/../../../demo-states/pricing-api/${local.initial_version}"
}

data "archive_file" "initial" {
  type        = "zip"
  output_path = "${path.module}/.terraform/build/pricing-api-${local.initial_version}.zip"

  dynamic "source" {
    for_each = ["handler.py", "VERSION", "openapi.yaml"]
    content {
      filename = source.value
      content  = file("${local.initial_source}/${source.value}")
    }
  }
  source {
    filename = "build_info.json"
    content  = jsonencode({ service = "pricing-api", version = local.initial_version, commit = "initial" })
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

resource "aws_iam_role" "function" {
  for_each           = local.pricing.environments
  name               = "${each.value.function}-function"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "function" {
  for_each          = local.pricing.environments
  name              = "/aws/lambda/${each.value.function}"
  retention_in_days = 14
}

# Logs only: the function reads nothing else.
data "aws_iam_policy_document" "function" {
  for_each = local.pricing.environments
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.function[each.key].arn}:*"]
  }
}

resource "aws_iam_role_policy" "function" {
  for_each = local.pricing.environments
  name     = "logs"
  role     = aws_iam_role.function[each.key].id
  policy   = data.aws_iam_policy_document.function[each.key].json
}

resource "aws_lambda_function" "pricing" {
  for_each         = local.pricing.environments
  function_name    = each.value.function
  description      = "pricing-api in ${each.key}. Demo system, not a client's."
  role             = aws_iam_role.function[each.key].arn
  runtime          = local.pricing.runtime
  handler          = local.pricing.handler
  memory_size      = 256
  timeout          = 5
  filename         = data.archive_file.initial.output_path
  source_code_hash = data.archive_file.initial.output_base64sha256
  publish          = true

  environment {
    variables = merge({ ENVIRONMENT = each.key }, lookup(each.value, "settings", {}))
  }

  # The pipeline deploys new code from JFrog Container Registry.
  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }

  depends_on = [aws_cloudwatch_log_group.function, aws_iam_role_policy.function]
}

resource "aws_lambda_alias" "live" {
  for_each         = local.pricing.environments
  name             = "live"
  function_name    = aws_lambda_function.pricing[each.key].function_name
  function_version = aws_lambda_function.pricing[each.key].version

  # The pipeline, its canary, rollbacks and reset.py move the alias.
  lifecycle {
    ignore_changes = [function_version, routing_config]
  }
}

# The API the consumers call. Every route goes to the alias live, so a
# canary's weights apply to API traffic as well.
resource "aws_apigatewayv2_api" "pricing" {
  for_each      = local.pricing.environments
  name          = each.value.function
  description   = "pricing-api in ${each.key}. Demo system, not a client's."
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "live" {
  for_each               = local.pricing.environments
  api_id                 = aws_apigatewayv2_api.pricing[each.key].id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_alias.live[each.key].invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 5000
}

resource "aws_apigatewayv2_route" "default" {
  for_each  = local.pricing.environments
  api_id    = aws_apigatewayv2_api.pricing[each.key].id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.live[each.key].id}"
}

# The API is public, like the store app's would be. A low throttle keeps a
# stranger's traffic cheap: the checks need a few requests, and the load
# generator calls the alias directly.
resource "aws_apigatewayv2_stage" "default" {
  for_each    = local.pricing.environments
  api_id      = aws_apigatewayv2_api.pricing[each.key].id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_rate_limit  = 10
    throttling_burst_limit = 20
  }
}

resource "aws_lambda_permission" "api" {
  for_each      = local.pricing.environments
  statement_id  = "AllowPricingApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.pricing[each.key].function_name
  qualifier     = aws_lambda_alias.live[each.key].name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.pricing[each.key].execution_arn}/*/*"
}

# Where the scripts find each API. Not secret, but kept out of the public
# repository like every other address.
resource "aws_ssm_parameter" "api_url" {
  for_each    = local.pricing.environments
  name        = "/release-demo/pricing-api/${each.key}/api-url"
  type        = "String"
  value       = aws_apigatewayv2_api.pricing[each.key].api_endpoint
  description = "pricing-api's API Gateway address in ${each.key}. Written by infra/demos/4-apis."
}

# The deploy role: assumed by the tools host's role, and by an administrator
# signed in through IAM Identity Center for runs from the Mac. It changes
# nothing but these two functions, their aliases and the canary's own alarms.
data "aws_iam_policy_document" "deploy_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:PrincipalArn"
      values = [
        "arn:aws:iam::${local.account_id}:role/release-demo-tools-host",
        "arn:aws:iam::${local.account_id}:role/aws-reserved/sso.amazonaws.com/*AWSReservedSSO_AdministratorAccess_*",
      ]
    }
  }
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid = "DeployAndRoute"
    actions = [
      "lambda:GetFunction",
      "lambda:GetFunctionConfiguration",
      "lambda:UpdateFunctionCode",
      "lambda:PublishVersion",
      "lambda:ListVersionsByFunction",
      "lambda:GetAlias",
      "lambda:UpdateAlias",
    ]
    resources = flatten([for f in aws_lambda_function.pricing : [f.arn, "${f.arn}:*"]])
  }
  statement {
    sid       = "LoadGeneratorCallsTheAlias"
    actions   = ["lambda:InvokeFunction"]
    resources = [for a in aws_lambda_alias.live : a.arn]
  }
  statement {
    sid       = "CanaryAlarms"
    actions   = ["cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms", "cloudwatch:TagResource"]
    resources = ["arn:aws:cloudwatch:${local.region}:${local.account_id}:alarm:pricing-api-canary-*"]
  }
  statement {
    sid       = "ReadAlarmsAndMetrics"
    actions   = ["cloudwatch:DescribeAlarms", "cloudwatch:GetMetricData", "cloudwatch:ListMetrics"]
    resources = ["*"]
  }
}

resource "aws_iam_role" "deploy" {
  name                 = "release-demo-deploy-pricing-api"
  assume_role_policy   = data.aws_iam_policy_document.deploy_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "deploy" {
  name   = "deploy-pricing-api"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}

output "api_urls" {
  description = "Each environment's API, which calls the alias live."
  value       = { for env, api in aws_apigatewayv2_api.pricing : env => api.api_endpoint }
}
