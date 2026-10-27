# quote-service on AWS: one Lambda function per environment, running the
# container image from ECR through the Lambda Web Adapter, published versions
# with the alias live, and a public function URL on that alias. Terraform ships
# 1.1.0 once; after that the pipeline deploys images and moves the alias, so
# Terraform ignores both. Settings come from services.yaml.
#
# The deploy role trusts the Jenkins issuer, not the tools host: the pipeline
# reaches AWS like the other two clouds, with the build's ID token.

locals {
  aws_targets = { for env, e in local.quote.environments : env => e.targets.aws }
}

resource "aws_ecr_repository" "quote" {
  name                 = "quote-service"
  image_tag_mutability = "IMMUTABLE"
  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "quote" {
  repository = aws_ecr_repository.quote.name
  policy = jsonencode({ rules = [
    {
      rulePriority = 1, description = "Keep the starting version for reset.py: no rule below expires it"
      selection    = { tagStatus = "tagged", tagPrefixList = [local.initial_version], countType = "imageCountMoreThan", countNumber = 100 }
      action       = { type = "expire" }
    },
    {
      rulePriority = 2, description = "Keep the last 50 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 50 }
      action       = { type = "expire" }
    },
  ] })
}

# Pushed by scripts/demos/clouds/seed.py before the functions exist.
data "aws_ecr_image" "initial" {
  repository_name = aws_ecr_repository.quote.name
  image_tag       = local.initial_version
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

resource "aws_iam_role" "quote" {
  for_each           = local.aws_targets
  name               = "${each.value.name}-function"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "quote" {
  for_each          = local.aws_targets
  name              = "/aws/lambda/${each.value.name}"
  retention_in_days = 14
}

data "aws_iam_policy_document" "quote_logs" {
  for_each = local.aws_targets
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.quote[each.key].arn}:*"]
  }
}

resource "aws_iam_role_policy" "quote" {
  for_each = local.aws_targets
  name     = "logs"
  role     = aws_iam_role.quote[each.key].id
  policy   = data.aws_iam_policy_document.quote_logs[each.key].json
}

resource "aws_lambda_function" "quote" {
  for_each      = local.aws_targets
  function_name = each.value.name
  role          = aws_iam_role.quote[each.key].arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.quote.repository_url}@${data.aws_ecr_image.initial.image_digest}"
  architectures = ["x86_64"]
  memory_size   = 512
  timeout       = 15
  publish       = true
  # The first published version takes the function's description, and the
  # pipeline finds versions by it: this one is 1.1.0. Later versions get their
  # own when the pipeline publishes them.
  description = local.initial_version

  environment {
    variables = merge({
      SERVICE_CLOUD       = "aws"
      SERVICE_ENVIRONMENT = each.key
      SERVICE_REGION      = local.region
    }, each.value.settings)
  }

  lifecycle {
    ignore_changes = [image_uri, description]
  }

  depends_on = [aws_cloudwatch_log_group.quote, aws_iam_role_policy.quote]
}

resource "aws_lambda_alias" "live" {
  for_each         = local.aws_targets
  name             = "live"
  function_name    = aws_lambda_function.quote[each.key].function_name
  function_version = aws_lambda_function.quote[each.key].version

  # The pipeline, rollbacks and reset.py move the alias.
  lifecycle {
    ignore_changes = [function_version]
  }
}

resource "aws_lambda_function_url" "quote" {
  for_each           = local.aws_targets
  function_name      = aws_lambda_function.quote[each.key].function_name
  qualifier          = aws_lambda_alias.live[each.key].name
  authorization_type = "NONE"
}

# A public function URL needs lambda:InvokeFunctionUrl, below, and
# lambda:InvokeFunction limited to calls through the URL, which
# aws_lambda_function_url adds itself.
resource "aws_lambda_permission" "quote_url" {
  for_each               = local.aws_targets
  statement_id           = "FunctionURLAllowPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.quote[each.key].function_name
  qualifier              = aws_lambda_alias.live[each.key].name
  principal              = "*"
  function_url_auth_type = "NONE"
}

# The deploy role. Only an ID token from the Jenkins issuer, for the job
# release, with the AWS audience, can assume it, for an hour at most.
data "aws_iam_policy_document" "quote_deploy_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.jenkins.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.issuer_host}:aud"
      values   = [local.aws_audience]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.issuer_host}:sub"
      values   = [local.jenkins_subject]
    }
  }
}

data "aws_iam_policy_document" "quote_deploy" {
  statement {
    sid       = "SignInToEcr"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid = "PushAndReadTheImage"
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload",
      "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:DescribeImages",
    ]
    resources = [aws_ecr_repository.quote.arn]
  }
  statement {
    sid = "DeployTheTwoFunctions"
    actions = [
      "lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:GetFunctionUrlConfig",
      "lambda:UpdateFunctionCode", "lambda:PublishVersion", "lambda:ListVersionsByFunction",
      "lambda:GetAlias", "lambda:UpdateAlias",
    ]
    resources = flatten([for f in aws_lambda_function.quote : [f.arn, "${f.arn}:*"]])
  }
}

resource "aws_iam_role" "quote_deploy" {
  name                 = "release-demo-deploy-quote-service"
  assume_role_policy   = data.aws_iam_policy_document.quote_deploy_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "quote_deploy" {
  name   = "deploy-quote-service"
  role   = aws_iam_role.quote_deploy.id
  policy = data.aws_iam_policy_document.quote_deploy.json
}

# What the scripts need to reach AWS, kept out of the public repository.
resource "aws_ssm_parameter" "aws" {
  name        = "/release-demo/clouds/aws"
  type        = "String"
  description = "quote-service on AWS: deploy role, registry, function URLs. Terraform, infra/demos/5-three-clouds."
  value = jsonencode({
    role_arn   = aws_iam_role.quote_deploy.arn
    registry   = split("/", aws_ecr_repository.quote.repository_url)[0]
    repository = aws_ecr_repository.quote.name
    urls       = { for env, url in aws_lambda_function_url.quote : env => trimsuffix(url.function_url, "/") }
  })
}

output "quote_service_aws_urls" {
  description = "The function URL of each environment's live alias."
  value       = { for env, url in aws_lambda_function_url.quote : env => url.function_url }
}
