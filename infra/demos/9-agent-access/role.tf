# The coding agent's role. It may read orders-service-staging's logs and
# configuration, and nothing more. Everything the video tries is denied
# explicitly, so no allow added later, in any policy, can open it, and AWS's
# refusal says "with an explicit deny in an identity-based policy".
#
# Who may assume it: your Identity Center AdministratorAccess role (video 9's
# agent.sh on the Mac) and the tools host's role (video 16's agent-task,
# which also needs infra/demos/16-agent-ship's permission on the host's side).
# Sessions last an hour at most.

data "aws_iam_policy_document" "coding_agent_trust" {
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
        # The path carries the Identity Center region: aws-reserved/sso.amazonaws.com/eu-west-1/.
        "arn:aws:iam::${local.account_id}:role/aws-reserved/sso.amazonaws.com/*AWSReservedSSO_AdministratorAccess_*",
        "arn:aws:iam::${local.account_id}:role/release-demo-tools-host",
      ]
    }
  }
}

resource "aws_iam_role" "coding_agent" {
  name                 = "release-demo-coding-agent"
  description          = "The coding agent: staging's logs and configuration only. Production, the platform's secrets, the deploy roles and IAM are denied explicitly."
  assume_role_policy   = data.aws_iam_policy_document.coding_agent_trust.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "coding_agent" {
  # What the agent may do: read staging.
  statement {
    sid     = "ReadStagingLogs"
    actions = ["logs:FilterLogEvents", "logs:GetLogEvents", "logs:DescribeLogStreams"]
    resources = [
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${local.staging}",
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${local.staging}:*",
      # GetLogEvents names the stream; IAM's wildcard above doesn't reach past its colon.
      "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${local.staging}:log-stream:*",
    ]
  }
  statement {
    sid     = "ReadStagingConfiguration"
    actions = ["lambda:GetFunctionConfiguration"]
    resources = [
      "arn:aws:lambda:${local.region}:${local.account_id}:function:${local.staging}",
      "arn:aws:lambda:${local.region}:${local.account_id}:function:${local.staging}:*",
    ]
  }

  # What it may never do, whatever else allows it.
  statement {
    sid     = "NoProductionFunctions"
    effect  = "Deny"
    actions = ["lambda:*"]
    # A function, then its versions and aliases.
    resources = [
      "arn:aws:lambda:*:${local.account_id}:function:*-production",
      "arn:aws:lambda:*:${local.account_id}:function:*-production:*",
    ]
  }
  statement {
    sid     = "NoProductionLogs"
    effect  = "Deny"
    actions = ["logs:*"]
    resources = [
      "arn:aws:logs:*:${local.account_id}:log-group:/aws/lambda/*-production",
      "arn:aws:logs:*:${local.account_id}:log-group:/aws/lambda/*-production:*",
      "arn:aws:logs:*:${local.account_id}:log-group:/aws/lambda/*-production:log-stream:*",
    ]
  }
  statement {
    sid     = "NoPlatformSecrets"
    effect  = "Deny"
    actions = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = [
      "arn:aws:ssm:*:${local.account_id}:parameter/release-demo",
      "arn:aws:ssm:*:${local.account_id}:parameter/release-demo/*",
    ]
  }
  statement {
    sid       = "NoPlatformRoles"
    effect    = "Deny"
    actions   = ["sts:AssumeRole"]
    resources = ["arn:aws:iam::${local.account_id}:role/release-demo-*"]
  }
  statement {
    sid       = "NoIAM"
    effect    = "Deny"
    actions   = ["iam:*"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "coding_agent" {
  name   = "staging-reads-only"
  role   = aws_iam_role.coding_agent.id
  policy = data.aws_iam_policy_document.coding_agent.json
}

output "coding_agent_role_arn" {
  description = "The role agent.sh and agent-task assume, with the session name coding-agent."
  value       = aws_iam_role.coding_agent.arn
}

output "coding_agent_policy" {
  description = "The role's policy, as AWS evaluates it: scripts/demos/agent-access/tests check it."
  value       = data.aws_iam_policy_document.coding_agent.json
}
