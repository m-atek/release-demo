# The deploy role the pipeline assumes from the tools host for store-assistant:
# MSIs into the demo's bucket, and Run Command on the Windows test instance
# only. Jamf is reached over HTTPS with its API client, not through AWS.

data "aws_iam_policy_document" "deploy_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
    # The tools host, and your own sign-in on the Mac, where reset.py desktop
    # runs (it reaches the pilot Mac over SSH), as demos 3 and 4 allow.
    condition {
      test     = "ArnLike"
      variable = "aws:PrincipalArn"
      values = [
        data.aws_iam_role.tools_host.arn,
        "arn:aws:iam::${local.account_id}:role/aws-reserved/sso.amazonaws.com/*AWSReservedSSO_AdministratorAccess_*",
      ]
    }
  }
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "Installers"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.desktop.arn}/installers/*"]
  }
  statement {
    sid       = "FindTheInstances"
    actions   = ["ec2:DescribeInstances"]
    resources = ["*"]
  }
  statement {
    sid       = "RunCommandOnTheTestInstance"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:aws:ec2:${local.region}:${local.account_id}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/Role"
      values   = ["desktop-test"]
    }
  }
  statement {
    sid       = "WithPowerShellOnly"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:aws:ssm:${local.region}::document/AWS-RunPowerShellScript"]
  }
  statement {
    sid       = "ReadTheCommandResult"
    actions   = ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"]
    resources = ["*"]
  }
}

resource "aws_iam_role" "deploy" {
  name                 = "release-demo-deploy-store-assistant"
  assume_role_policy   = data.aws_iam_policy_document.deploy_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "deploy" {
  name   = "deploy-store-assistant"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}
