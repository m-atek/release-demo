# The AWS side: the service principal's id and the two pointers in SSM, and
# the deploy role that writes the pointers. The tools host's own role reads
# every /release-demo/ parameter already; it may assume this role to write
# the two pointers and read their history (/rollback), and nothing else.

resource "aws_ssm_parameter" "client_id" {
  name        = "/release-demo/databricks/client-id"
  type        = "String"
  value       = databricks_service_principal.pipeline.application_id
  description = substr(local.parameters["databricks/client-id"].help, 0, 1024)
}

# Written by scripts/demos/rag/deploy.py; Terraform only makes the names.
resource "aws_ssm_parameter" "pointer" {
  for_each    = toset(local.pointers)
  name        = "/release-demo/${each.key}"
  type        = "String"
  value       = "unset"
  description = substr(local.parameters[each.key].help, 0, 1024)

  lifecycle {
    ignore_changes = [value]
  }
}

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
    sid       = "ThePointers"
    actions   = ["ssm:PutParameter", "ssm:GetParameter", "ssm:GetParameterHistory"]
    resources = [for name in local.pointers : "arn:aws:ssm:${local.region}:${local.account_id}:parameter/release-demo/${name}"]
  }
}

resource "aws_iam_role" "deploy" {
  name                 = local.databricks.deploy_role
  assume_role_policy   = data.aws_iam_policy_document.deploy_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "deploy" {
  name   = "runbook-assistant-pointers"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}
