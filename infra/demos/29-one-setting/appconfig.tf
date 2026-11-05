# The settings file: one application, an environment per orders-service
# environment, one free form profile with a JSON Schema validator, and two
# deployment strategies. Staging takes a version all at once. Production takes it
# all at once too, then bakes for app_settings.bake_minutes while its environment
# watches demo 21's alarm, and AWS AppConfig puts the version before it back if the
# alarm goes into ALARM. No percentage rollout: AWS AppConfig's percentage counts
# clients as they poll, which on a few Lambda containers isn't a share of requests.

data "aws_iam_role" "appconfig_monitor" {
  name = "release-demo-appconfig-alarm" # infra/demos/21-release-train-flags: cloudwatch:DescribeAlarms only
}

resource "aws_appconfig_application" "settings" {
  name        = local.settings.application
  description = "orders-service's settings file (video 29)."
}

resource "aws_appconfig_environment" "settings" {
  for_each       = local.orders.environments
  name           = each.key
  application_id = aws_appconfig_application.settings.id
  description    = "orders-service in ${each.key}."

  dynamic "monitor" {
    for_each = each.key == "production" ? [local.alarm_arn] : []
    content {
      alarm_arn      = monitor.value
      alarm_role_arn = data.aws_iam_role.appconfig_monitor.arn
    }
  }
}

resource "aws_appconfig_configuration_profile" "settings" {
  application_id = aws_appconfig_application.settings.id
  name           = local.settings.profile
  description    = "orders-service's settings file. AWS AppConfig's validator refuses a file that doesn't match the schema."
  location_uri   = "hosted"
  type           = "AWS.Freeform"

  validator {
    type    = "JSON_SCHEMA"
    content = file("${path.module}/settings.schema.json")
  }
}

# The first version: the baseline. The job setting and reset.py add the versions
# after it, so Terraform keeps only this one.
resource "aws_appconfig_hosted_configuration_version" "first" {
  application_id           = aws_appconfig_application.settings.id
  configuration_profile_id = aws_appconfig_configuration_profile.settings.configuration_profile_id
  description              = "The baseline of services.yaml"
  content_type             = "application/json"
  content                  = jsonencode(local.settings.baseline)

  lifecycle {
    ignore_changes = [content]
  }
}

resource "aws_appconfig_deployment_strategy" "quick" {
  name                           = "release-demo-settings-quick"
  description                    = "Staging, and reset.py: all at once, no bake time."
  deployment_duration_in_minutes = 0
  growth_factor                  = 100
  growth_type                    = "LINEAR"
  final_bake_time_in_minutes     = 0
  replicate_to                   = "NONE"
}

resource "aws_appconfig_deployment_strategy" "bake" {
  name                           = "release-demo-settings"
  description                    = "Production: all at once, then ${local.settings.bake_minutes} minutes of bake time watching the alarm."
  deployment_duration_in_minutes = 0
  growth_factor                  = 100
  growth_type                    = "LINEAR"
  final_bake_time_in_minutes     = local.settings.bake_minutes
  replicate_to                   = "NONE"
}

# Both environments start on the baseline, so the functions' first read has an answer.
resource "aws_appconfig_deployment" "first" {
  for_each                 = aws_appconfig_environment.settings
  application_id           = aws_appconfig_application.settings.id
  environment_id           = each.value.environment_id
  configuration_profile_id = aws_appconfig_configuration_profile.settings.configuration_profile_id
  configuration_version    = aws_appconfig_hosted_configuration_version.first.version_number
  deployment_strategy_id   = aws_appconfig_deployment_strategy.quick.id
  description              = "Terraform: the baseline"
}

locals {
  application_arn = aws_appconfig_application.settings.arn
  profile_arn     = "${local.application_arn}/configurationprofile/${aws_appconfig_configuration_profile.settings.configuration_profile_id}"
  environment_arns = {
    for name, environment in aws_appconfig_environment.settings : name => "${local.application_arn}/environment/${environment.environment_id}"
  }
}
