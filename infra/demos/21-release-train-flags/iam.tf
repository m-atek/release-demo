# Who may read or switch orders-service's flags. Each grant names this
# application's own environments, profile and strategies, nothing else.
#
# - Each function's role reads only its own environment's flags, through
#   AppConfig's data API.
# - The Slack handler (/flag) writes a new version of the profile and deploys
#   it to production with the switch strategy, and reads the ids from SSM.
# - The tools host's role reads production's flags for the release train,
#   and switches flags in both environments for reset.py.

data "aws_iam_role" "function" {
  for_each = local.orders.environments
  name     = "${each.value.function}-function"
}

data "aws_iam_role" "slack_handler" {
  name = "release-demo-slack-handler"
}

data "aws_iam_role" "tools_host" {
  name = "release-demo-tools-host"
}

locals {
  data_api_actions = ["appconfig:StartConfigurationSession", "appconfig:GetLatestConfiguration"]
  version_actions = ["appconfig:ListHostedConfigurationVersions", "appconfig:GetHostedConfigurationVersion",
  "appconfig:CreateHostedConfigurationVersion"]
  configuration_arns = {
    for name, arn in local.environment_arns : name => "${arn}/configuration/${aws_appconfig_configuration_profile.flags.configuration_profile_id}"
  }
  strategy_arn       = "arn:aws:appconfig:${local.region}:${local.account_id}:deploymentstrategy/${aws_appconfig_deployment_strategy.flags.id}"
  reset_strategy_arn = "arn:aws:appconfig:${local.region}:${local.account_id}:deploymentstrategy/${aws_appconfig_deployment_strategy.reset.id}"
  ids_parameter      = "arn:aws:ssm:${local.region}:${local.account_id}:parameter/release-demo/appconfig/orders-service"
}

data "aws_iam_policy_document" "function_flags" {
  for_each = local.orders.environments
  statement {
    sid       = "ReadOwnFlags"
    actions   = local.data_api_actions
    resources = [local.configuration_arns[each.key]]
  }
}

resource "aws_iam_role_policy" "function_flags" {
  for_each = local.orders.environments
  name     = "read-own-flags"
  role     = data.aws_iam_role.function[each.key].name
  policy   = data.aws_iam_policy_document.function_flags[each.key].json
}

data "aws_iam_policy_document" "slack_handler" {
  statement {
    sid       = "ReadTheIds"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = [local.ids_parameter]
  }
  statement {
    sid       = "WriteAVersionOfTheFlags"
    actions   = local.version_actions
    resources = [local.profile_arn, "${local.profile_arn}/hostedconfigurationversion/*"]
  }
  statement {
    sid       = "DeployItToProduction"
    actions   = ["appconfig:StartDeployment"]
    resources = [local.appconfig_arn, local.environment_arns.production, local.profile_arn, local.strategy_arn]
  }
  statement {
    sid       = "TagTheDeployment"
    actions   = ["appconfig:TagResource"]
    resources = ["${local.environment_arns.production}/deployment/*"]
  }
}

resource "aws_iam_role_policy" "slack_handler" {
  name   = "flag-switches-in-production"
  role   = data.aws_iam_role.slack_handler.name
  policy = data.aws_iam_policy_document.slack_handler.json
}

data "aws_iam_policy_document" "tools_host" {
  statement {
    sid       = "ReadTheFlagsLikeTheFunctions"
    actions   = local.data_api_actions
    resources = values(local.configuration_arns)
  }
  statement {
    sid       = "ReadAndWriteVersionsOfTheFlags"
    actions   = local.version_actions
    resources = [local.profile_arn, "${local.profile_arn}/hostedconfigurationversion/*"]
  }
  statement {
    sid     = "DeployStopAndListInBothEnvironments"
    actions = ["appconfig:StartDeployment", "appconfig:ListDeployments"]
    resources = concat([local.appconfig_arn, local.profile_arn, local.strategy_arn, local.reset_strategy_arn],
    values(local.environment_arns))
  }
  statement {
    sid       = "TheDeploymentsThemselves"
    actions   = ["appconfig:GetDeployment", "appconfig:StopDeployment", "appconfig:TagResource"]
    resources = [for arn in values(local.environment_arns) : "${arn}/deployment/*"]
  }
}

resource "aws_iam_role_policy" "tools_host" {
  name   = "orders-service-flags"
  role   = data.aws_iam_role.tools_host.name
  policy = data.aws_iam_policy_document.tools_host.json
}
