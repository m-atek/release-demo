# The Jamf Pro stand-in: scripts/demos/standins/jamf.py as a Lambda function
# behind a function URL, with its state and files in the demo's bucket. It
# answers the Jamf Pro API and Classic API calls the pipeline makes, the
# pilot Mac's agent, and its own page. Its secrets are generated here and
# read from SSM at cold start; the URL needs an API client's token, a
# device's token or a signed page link for everything but the token request.

data "archive_file" "jamf_standin" {
  type        = "zip"
  output_path = "${path.module}/.build/jamf-standin.zip"
  source {
    filename = "jamf.py"
    content  = file("${path.module}/../../../scripts/demos/standins/jamf.py")
  }
}

resource "random_password" "jamf_standin" {
  for_each = toset(["client-secret", "signing-key", "enrollment-code"])
  length   = 40
  special  = false
}

resource "random_uuid" "jamf_standin_client_id" {}

locals {
  jamf_secrets = {
    "jamf/standin-client-id"       = random_uuid.jamf_standin_client_id.result
    "jamf/standin-client-secret"   = random_password.jamf_standin["client-secret"].result
    "jamf/standin-signing-key"     = random_password.jamf_standin["signing-key"].result
    "jamf/standin-enrollment-code" = random_password.jamf_standin["enrollment-code"].result
  }
}

resource "aws_ssm_parameter" "jamf_standin" {
  for_each    = local.jamf_secrets
  name        = "/release-demo/${each.key}"
  type        = "SecureString"
  value       = each.value
  description = substr(local.parameters[each.key].help, 0, 1024)
}

resource "aws_ssm_parameter" "jamf_standin_url" {
  name        = "/release-demo/jamf/standin-url"
  type        = "SecureString"
  value       = trimsuffix(aws_lambda_function_url.jamf_standin.function_url, "/")
  description = substr(local.parameters["jamf/standin-url"].help, 0, 1024)
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

resource "aws_iam_role" "jamf_standin" {
  name               = "release-demo-jamf-standin"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "jamf_standin" {
  name              = "/aws/lambda/release-demo-jamf-standin"
  retention_in_days = 14
}

data "aws_iam_policy_document" "jamf_standin" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.jamf_standin.arn}:*"]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.desktop.arn}/state.json", "${aws_s3_bucket.desktop.arn}/files/*"]
  }
  statement {
    # GetObject on a missing state.json answers NoSuchKey only with ListBucket.
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.desktop.arn]
  }
  statement {
    actions   = ["ssm:GetParameter"]
    resources = [for name in keys(local.jamf_secrets) : "arn:aws:ssm:${local.region}:${local.account_id}:parameter/release-demo/${name}"]
  }
}

resource "aws_iam_role_policy" "jamf_standin" {
  name   = "state-files-and-secrets"
  role   = aws_iam_role.jamf_standin.id
  policy = data.aws_iam_policy_document.jamf_standin.json
}

resource "aws_lambda_function" "jamf_standin" {
  function_name    = "release-demo-jamf-standin"
  description      = "Jamf Pro stand-in for demos 6 and 7. Demo system, not a client's."
  role             = aws_iam_role.jamf_standin.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "jamf.lambda_handler"
  memory_size      = 512
  timeout          = 30
  filename         = data.archive_file.jamf_standin.output_path
  source_code_hash = data.archive_file.jamf_standin.output_base64sha256

  environment {
    variables = {
      BUCKET           = aws_s3_bucket.desktop.bucket
      PARAMETER_PREFIX = "/release-demo/jamf/standin-"
    }
  }

  depends_on = [aws_cloudwatch_log_group.jamf_standin, aws_iam_role_policy.jamf_standin, aws_ssm_parameter.jamf_standin]
}

resource "aws_lambda_function_url" "jamf_standin" {
  function_name      = aws_lambda_function.jamf_standin.function_name
  authorization_type = "NONE"
}

# Since October 2025 a public function URL needs lambda:InvokeFunctionUrl here
# and lambda:InvokeFunction through the URL, which aws_lambda_function_url adds.
resource "aws_lambda_permission" "jamf_standin_url" {
  statement_id           = "FunctionURLAllowPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.jamf_standin.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}
