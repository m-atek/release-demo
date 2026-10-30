# Three roles, each trusting exactly one subject of GitHub's OIDC token, with
# audience sts.amazonaws.com:
#
# - release-demo-github-ci: jobs on main without an environment, the gates,
#   Publish, Change and Stopped. It reads their SSM parameters, and changes
#   nothing in AWS.
# - release-demo-github-staging: jobs in the environment staging. It updates
#   orders-service-staging and its alias.
# - release-demo-github-production: jobs in the environment production, which
#   GitHub starts only once the approval bridge has the change's approval. It
#   updates orders-service-production and its alias.
#
# GitHub issues an environment's subject only to a job of that environment,
# and the environments deploy from main only, so no other branch, fork or job
# gets these roles. deploy.py uses the job's role instead of assuming
# release-demo-deploy-orders-service, which can change both functions.

locals {
  # Every job: Slack's message, the events to Grafana, the change's work notes, the run state's key.
  common_parameters = [
    "slack/bot-token", "slack/channel-id",
    "grafana/logs-url", "grafana/logs-user", "grafana/logs-token",
    "servicenow/instance-url", "servicenow/release-pipeline-password",
    "github/run-state-key",
  ]
  # JFrog Container Registry, through Cloudflare Access.
  jfrog_parameters = ["jcr/token", "cloudflare/github-actions-client-id", "cloudflare/github-actions-client-secret"]

  roles = {
    ci = {
      subject     = "repo:${local.repository}:ref:refs/heads/main"
      environment = null
      parameters = concat(local.common_parameters, local.jfrog_parameters, [
        "jira/site-url", "jira/email", "jira/api-token",
        "sonar/organization", "sonar/token",
        "grafana/stack-url",
        "slack/member-id/*", # who asked: the releaser whose github_login merged (scripts/ci_env.py)
      ])
    }
    staging = {
      subject     = "repo:${local.repository}:environment:staging"
      environment = "staging"
      parameters  = concat(local.common_parameters, local.jfrog_parameters)
    }
    production = {
      subject     = "repo:${local.repository}:environment:production"
      environment = "production"
      parameters = concat(local.common_parameters, local.jfrog_parameters, [
        "jira/site-url", "jira/email", "jira/api-token",
        "grafana/stack-url", "grafana/service-account-token",
        "github/app-id", "github/app-private-key", "github/repository", # tag.py, as the GitHub App
      ])
    }
  }
}

data "aws_iam_policy_document" "trust" {
  for_each = local.roles

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [each.value.subject]
    }
  }
}

resource "aws_iam_role" "github" {
  for_each             = local.roles
  name                 = "release-demo-github-${each.key}"
  description          = "release.yml's jobs that GitHub's OIDC token names ${each.value.subject}."
  assume_role_policy   = data.aws_iam_policy_document.trust[each.key].json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "github" {
  for_each = local.roles

  statement {
    sid       = "ReadTheJobsParameters"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = [for name in each.value.parameters : "arn:aws:ssm:${local.region}:${local.account_id}:parameter/release-demo/${name}"]
  }

  # The environment's function only, as release-demo-deploy-orders-service allows for both.
  dynamic "statement" {
    for_each = each.value.environment == null ? [] : [local.orders.environments[each.value.environment].function]
    content {
      sid = "DeployTheEnvironmentsFunction"
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
      resources = [
        "arn:aws:lambda:${local.region}:${local.account_id}:function:${statement.value}",
        "arn:aws:lambda:${local.region}:${local.account_id}:function:${statement.value}:*",
      ]
    }
  }
}

resource "aws_iam_role_policy" "github" {
  for_each = local.roles
  name     = each.value.environment == null ? "gate-parameters" : "deploy-${each.value.environment}"
  role     = aws_iam_role.github[each.key].id
  policy   = data.aws_iam_policy_document.github[each.key].json
}
