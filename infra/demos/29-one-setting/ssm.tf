# What the job setting and reset.py read: the ids, which AWS AppConfig's control plane wants instead of names.

locals {
  appconfig_ids = {
    application  = aws_appconfig_application.settings.id
    environments = { for name, environment in aws_appconfig_environment.settings : name => environment.environment_id }
    profile      = aws_appconfig_configuration_profile.settings.configuration_profile_id
    quick        = aws_appconfig_deployment_strategy.quick.id
    bake         = aws_appconfig_deployment_strategy.bake.id
  }
}

resource "aws_ssm_parameter" "appconfig" {
  name        = "/release-demo/appconfig/orders-service-settings"
  type        = "String"
  description = "Video 29. orders-service's settings file in AWS AppConfig: its ids, as JSON. Written by infra/demos/29-one-setting."
  value       = jsonencode(local.appconfig_ids)
}

output "appconfig" {
  description = "The settings file's AWS AppConfig ids."
  value       = local.appconfig_ids
}
