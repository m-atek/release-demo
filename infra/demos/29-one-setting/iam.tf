# Who may read or change the settings file. Each grant names this application's
# own environments, profile and strategies, nothing else. AWS AppConfig authorizes a
# call on every resource type the Service Authorization Reference marks required
# (demo 21 found this out), so each statement names them all.
#
# - Each function's role reads only its own environment's file, through
#   AWS AppConfig's data API.
# - The tools host's role, where the job setting and reset.py run, reads and writes
#   versions of the file, runs the validator, and deploys it to both environments.

data "aws_iam_role" "function" {
  for_each = local.orders.environments
  name     = "${each.value.function}-function"
}

data "aws_iam_role" "tools_host" {
  name = "release-demo-tools-host"
}

locals {
  configuration_arns = {
    for name, arn in local.environment_arns : name => "${arn}/configuration/${aws_appconfig_configuration_profile.settings.configuration_profile_id}"
  }
  strategy_arns = [
    "arn:aws:appconfig:${local.region}:${local.account_id}:deploymentstrategy/${aws_appconfig_deployment_strategy.quick.id}",
    "arn:aws:appconfig:${local.region}:${local.account_id}:deploymentstrategy/${aws_appconfig_deployment_strategy.bake.id}",
  ]
}

data "aws_iam_policy_document" "function" {
  for_each = local.orders.environments
  statement {
    sid       = "ReadOwnSettings"
    actions   = ["appconfig:StartConfigurationSession", "appconfig:GetLatestConfiguration"]
    resources = [local.configuration_arns[each.key]]
  }
}

resource "aws_iam_role_policy" "function" {
  for_each = local.orders.environments
  name     = "read-own-settings"
  role     = data.aws_iam_role.function[each.key].name
  policy   = data.aws_iam_policy_document.function[each.key].json
}

data "aws_iam_policy_document" "tools_host" {
  statement {
    sid = "VersionsOfTheFileAndItsValidator"
    actions = ["appconfig:ListHostedConfigurationVersions", "appconfig:GetHostedConfigurationVersion",
    "appconfig:CreateHostedConfigurationVersion", "appconfig:ValidateConfiguration"]
    resources = [local.application_arn, local.profile_arn, "${local.profile_arn}/hostedconfigurationversion/*"]
  }
  statement {
    sid       = "DeployAndListInBothEnvironments"
    actions   = ["appconfig:StartDeployment", "appconfig:ListDeployments"]
    resources = concat([local.application_arn, local.profile_arn], local.strategy_arns, values(local.environment_arns))
  }
  statement {
    sid     = "TheDeploymentsThemselves"
    actions = ["appconfig:GetDeployment", "appconfig:StopDeployment"]
    resources = concat([local.application_arn], values(local.environment_arns),
    [for arn in values(local.environment_arns) : "${arn}/deployment/*"])
  }
}

resource "aws_iam_role_policy" "tools_host" {
  name   = "orders-service-settings"
  role   = data.aws_iam_role.tools_host.name
  policy = data.aws_iam_policy_document.tools_host.json
}
