# What the Slack handler and the scripts read: the ids, which AppConfig's
# control plane wants instead of names, and the train's switch.

locals {
  appconfig_ids = {
    application    = aws_appconfig_application.orders.id
    environments   = { for name, environment in aws_appconfig_environment.orders : name => environment.environment_id }
    profile        = aws_appconfig_configuration_profile.flags.configuration_profile_id
    strategy       = aws_appconfig_deployment_strategy.flags.id
    reset_strategy = aws_appconfig_deployment_strategy.reset.id
  }
}

resource "aws_ssm_parameter" "appconfig" {
  name        = "/release-demo/appconfig/orders-service"
  type        = "String"
  description = "Demo 21. orders-service's AWS AppConfig ids, as JSON. Written by infra/demos/21-release-train-flags."
  value       = jsonencode(local.appconfig_ids)
}

resource "aws_ssm_parameter" "train_enabled" {
  name        = "/release-demo/release-train/enabled"
  type        = "String"
  value       = "off"
  description = "Demo 21. on or off: whether the release train leaves at its departures. scripts/demo.sh up release-train sets on, down off."

  lifecycle {
    ignore_changes = [value]
  }
}

output "appconfig" {
  description = "orders-service's AWS AppConfig ids."
  value       = local.appconfig_ids
}
