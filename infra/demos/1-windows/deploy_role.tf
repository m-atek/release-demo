# The deploy role for store-sync: assumed by the tools host's role only, like
# orders-service's. It runs PowerShell through Run Command on this fleet and
# nowhere else, reads the command results, and keeps the fleet's read-only JCR
# token in SSM (scripts/demos/windows/jcr_token.py, on the tools host).

data "aws_iam_policy_document" "deploy_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = [data.aws_iam_role.tools_host.arn]
    }
  }
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "RunPowerShellOnTheFleetOnly"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:aws:ec2:${local.region}:${local.account_id}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/Demo"
      values   = ["1-windows"]
    }
  }
  statement {
    sid       = "WithThisDocumentOnly"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:aws:ssm:${local.region}::document/AWS-RunPowerShellScript"]
  }
  statement {
    sid = "ReadResults"
    actions = [
      "ssm:GetCommandInvocation",
      "ssm:ListCommandInvocations",
      "ssm:ListCommands",
      "ssm:DescribeInstanceInformation",
      "ec2:DescribeInstances",
    ]
    resources = ["*"]
  }
  statement {
    sid       = "KeepTheFleetToken"
    actions   = ["ssm:GetParameter", "ssm:PutParameter"]
    resources = [aws_ssm_parameter.token.arn]
  }
}

resource "aws_iam_role" "deploy" {
  name                 = "release-demo-deploy-store-sync"
  assume_role_policy   = data.aws_iam_policy_document.deploy_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "deploy" {
  name   = "deploy-store-sync"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}
