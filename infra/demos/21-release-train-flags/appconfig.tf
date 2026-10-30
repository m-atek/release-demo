# orders-service's flags in AWS AppConfig: one application, an environment per
# orders-service environment, one feature flags profile, and two deployment
# strategies. A switch (/flag) bakes for 10 minutes while production's
# environment watches the alarm, and AppConfig rolls it back if the alarm goes
# into ALARM; reset.py's own switches go all at once, with no bake time.

resource "aws_appconfig_application" "orders" {
  name        = local.flags.application
  description = "orders-service's feature flags (demo 21)."
}

resource "aws_appconfig_environment" "orders" {
  for_each       = local.orders.environments
  name           = each.key
  application_id = aws_appconfig_application.orders.id
  description    = "orders-service in ${each.key}."

  dynamic "monitor" {
    for_each = each.key == "production" ? [aws_cloudwatch_metric_alarm.production_errors.arn] : []
    content {
      alarm_arn      = monitor.value
      alarm_role_arn = aws_iam_role.appconfig_monitor.arn
    }
  }
}

resource "aws_appconfig_configuration_profile" "flags" {
  application_id = aws_appconfig_application.orders.id
  name           = local.flags.profile
  description    = "orders-service's feature flags; each version's flags.yaml names them and the issue each hides."
  location_uri   = "hosted"
  type           = "AWS.AppConfig.FeatureFlags"
}

# The first version: every flag of flags.yaml, off. /flag and reset.py add the
# versions after it, so Terraform keeps only this one.
resource "aws_appconfig_hosted_configuration_version" "first" {
  application_id           = aws_appconfig_application.orders.id
  configuration_profile_id = aws_appconfig_configuration_profile.flags.configuration_profile_id
  description              = "The flags of flags.yaml, every one off"
  content_type             = "application/json"
  content = jsonencode({
    version = "1"
    flags   = { for name, flag in local.declared : name => { name = name, description = flag.description } }
    values  = { for name, flag in local.declared : name => { enabled = false } }
  })

  # AppConfig stores a feature flags version with _createdAt and _updatedAt
  # added to each flag, so the content read back never equals this one. Without
  # this, every plan replaces the version and both first deployments, and an
  # apply would deploy the flags again (a change in ServiceNow, a Slack line).
  lifecycle {
    ignore_changes = [content]
  }
}

resource "aws_appconfig_deployment_strategy" "flags" {
  name                           = local.flags.strategy
  description                    = "A flag switch: all at once, then ${local.watch_minutes} minutes of bake time watching the alarm."
  deployment_duration_in_minutes = 0
  growth_factor                  = 100
  growth_type                    = "LINEAR"
  final_bake_time_in_minutes     = local.watch_minutes
  replicate_to                   = "NONE"
}

resource "aws_appconfig_deployment_strategy" "reset" {
  name                           = "${local.flags.strategy}-reset"
  description                    = "reset.py's switches: all at once, no bake time, so the next take can switch a flag at once."
  deployment_duration_in_minutes = 0
  growth_factor                  = 100
  growth_type                    = "LINEAR"
  final_bake_time_in_minutes     = 0
  replicate_to                   = "NONE"
}

# Both environments start on the first version, so the functions' first read
# has an answer. Production's goes on the record like any other: the event
# bridge's rule comes first.
resource "aws_appconfig_deployment" "first" {
  for_each                 = aws_appconfig_environment.orders
  application_id           = aws_appconfig_application.orders.id
  environment_id           = each.value.environment_id
  configuration_profile_id = aws_appconfig_configuration_profile.flags.configuration_profile_id
  configuration_version    = aws_appconfig_hosted_configuration_version.first.version_number
  deployment_strategy_id   = aws_appconfig_deployment_strategy.reset.id
  description              = "Terraform: the first version of the flags, every one off"
  tags                     = { requested_by = "terraform" }

  depends_on = [aws_cloudwatch_event_target.bridge, aws_appconfig_extension_association.events,
  aws_lambda_permission.events]
}

locals {
  appconfig_arn = aws_appconfig_application.orders.arn
  profile_arn   = "${local.appconfig_arn}/configurationprofile/${aws_appconfig_configuration_profile.flags.configuration_profile_id}"
  environment_arns = {
    for name, environment in aws_appconfig_environment.orders : name => "${local.appconfig_arn}/environment/${environment.environment_id}"
  }
}
