# Demo 5 on Azure: quote-service in Azure Container Apps, staging and
# production, pulling from Azure Container Registry with a managed identity.
#
# Two layers. Always there, made once by scripts/demos/clouds/bootstrap.sh
# azure with your az login: the resource group, the deploy identity Jenkins
# signs in as (a federated credential that trusts the Jenkins issuer, for the
# job release only), the identity the apps pull with, and the budget alert.
# With apps = true, from scripts/demo.sh up clouds: the registry (billed per
# day while it exists), the Container Apps environment and both apps. down
# sets apps = false, which deletes them.
#
# Run it through scripts/demos/clouds/tf.sh azure ..., which takes the
# subscription from SSM and signs in to AWS for the state and the issuer.

terraform {
  required_version = ">= 1.10"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 5.9.0" }
    aws     = { source = "hashicorp/aws", version = "~> 6.69.0" }
    random  = { source = "hashicorp/random", version = "~> 3.9.0" }
    time    = { source = "hashicorp/time", version = "~> 0.13.0" }
  }
  backend "s3" {
    key          = "release-demo/demos/5-three-clouds-azure.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

# The subscription comes from ARM_SUBSCRIPTION_ID, which tf.sh reads from SSM;
# your az login signs in.
provider "azurerm" {
  features {}
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "5-three-clouds", ManagedBy = "terraform" }
  }
}

variable "apps" {
  description = "The registry, the Container Apps environment and both apps. scripts/demo.sh up clouds sets true, down false."
  type        = bool
  default     = false
}

variable "budget_email" {
  description = "Who gets the budget alert. From infra/local.auto.tfvars, through tf.sh."
  type        = string
}

data "aws_ssm_parameter" "issuer" {
  name = "/release-demo/jenkins/oidc-issuer"
}

# The image every app starts on, 1.1.0, by digest: seed.py copies it from ECR.
data "aws_ecr_image" "initial" {
  repository_name = "quote-service"
  image_tag       = "1.1.0"
}

data "azurerm_client_config" "current" {}

locals {
  quote    = yamldecode(file("${path.module}/../../../../services.yaml"))["quote-service"]
  region   = one([for c in local.quote.clouds : c.region if c.key == "azure"])
  targets  = { for env, e in local.quote.environments : env => e.targets.azure }
  apps     = var.apps ? local.targets : {}
  subject  = "https://jenkins-demo.es.nl/job/release/"
  audience = "api://AzureADTokenExchange"
  tags     = { project = "release-demo", demo = "5-three-clouds" }
}

resource "azurerm_resource_group" "quote" {
  name     = "release-demo-quote-service"
  location = local.region
  tags     = local.tags
}

# Jenkins's identity in Azure. No secret and no certificate: Entra trusts an
# ID token from the Jenkins issuer for the job release, with the Azure audience.
resource "azurerm_user_assigned_identity" "deployer" {
  name                = "quote-service-deployer"
  resource_group_name = azurerm_resource_group.quote.name
  location            = local.region
  tags                = local.tags
}

resource "azurerm_federated_identity_credential" "jenkins" {
  name                      = "jenkins-release-job"
  user_assigned_identity_id = azurerm_user_assigned_identity.deployer.id
  audience                  = [local.audience]
  issuer                    = nonsensitive(data.aws_ssm_parameter.issuer.value)
  subject                   = local.subject
}

# The apps pull their image with this identity, never with a registry password.
resource "azurerm_user_assigned_identity" "apps" {
  name                = "quote-service-apps"
  resource_group_name = azurerm_resource_group.quote.name
  location            = local.region
  tags                = local.tags
}

# An app update names the pull identity, so the deployer may assign it.
resource "azurerm_role_assignment" "deployer_assigns_pull_identity" {
  scope                = azurerm_user_assigned_identity.apps.id
  role_definition_name = "Managed Identity Operator"
  principal_id         = azurerm_user_assigned_identity.deployer.principal_id
}

resource "azurerm_consumption_budget_subscription" "monthly" {
  name            = "release-demo-monthly"
  subscription_id = "/subscriptions/${data.azurerm_client_config.current.subscription_id}"
  amount          = 10
  time_grain      = "Monthly"

  time_period {
    start_date = formatdate("YYYY-MM-01'T'00:00:00Z", timestamp())
  }

  notification {
    operator       = "GreaterThan"
    threshold      = 100
    threshold_type = "Actual"
    contact_emails = [var.budget_email]
  }

  notification {
    operator       = "GreaterThan"
    threshold      = 100
    threshold_type = "Forecasted"
    contact_emails = [var.budget_email]
  }

  lifecycle {
    ignore_changes = [time_period]
  }
}

# Up -------------------------------------------------------------------------

resource "random_id" "registry" {
  byte_length = 3
}

resource "azurerm_container_registry" "quote" {
  count               = var.apps ? 1 : 0
  name                = "releasedemoquotes${random_id.registry.hex}"
  resource_group_name = azurerm_resource_group.quote.name
  location            = local.region
  sku                 = "Basic"
  admin_enabled       = false
  tags                = local.tags
}

resource "azurerm_role_assignment" "apps_pull" {
  count                = var.apps ? 1 : 0
  scope                = azurerm_container_registry.quote[0].id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.apps.principal_id
}

resource "azurerm_role_assignment" "deployer_push" {
  count                = var.apps ? 1 : 0
  scope                = azurerm_container_registry.quote[0].id
  role_definition_name = "AcrPush"
  principal_id         = azurerm_user_assigned_identity.deployer.principal_id
}

# A new role assignment takes a minute to reach the registry.
resource "time_sleep" "pull_role" {
  count           = var.apps ? 1 : 0
  create_duration = "60s"
  depends_on      = [azurerm_role_assignment.apps_pull]
}

# Consumption only, and no Log Analytics workspace: nothing is billed for log
# ingestion. Each app's console log stays readable in the portal's log stream.
resource "azurerm_container_app_environment" "quote" {
  count               = var.apps ? 1 : 0
  name                = "quote-service"
  resource_group_name = azurerm_resource_group.quote.name
  location            = local.region
  tags                = local.tags
}

resource "azurerm_container_app" "quote" {
  for_each                     = local.apps
  name                         = each.value.name
  resource_group_name          = azurerm_resource_group.quote.name
  container_app_environment_id = azurerm_container_app_environment.quote[0].id
  # Several revisions stay active, so going back is moving traffic.
  revision_mode = "Multiple"
  tags          = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.apps.id]
  }

  registry {
    server   = azurerm_container_registry.quote[0].login_server
    identity = azurerm_user_assigned_identity.apps.id
  }

  ingress {
    external_enabled = true
    target_port      = local.quote.image.port
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas    = 0
    max_replicas    = 1
    revision_suffix = "v1-1-0-initial"

    container {
      name   = "quote-service"
      image  = "${azurerm_container_registry.quote[0].login_server}/quote-service@${data.aws_ecr_image.initial.image_digest}"
      cpu    = 0.25
      memory = "0.5Gi"

      dynamic "env" {
        for_each = merge({ SERVICE_CLOUD = "azure", SERVICE_ENVIRONMENT = each.key, SERVICE_REGION = local.region },
        each.value.settings)
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  # The pipeline deploys new revisions and moves traffic; reset.py moves it back.
  lifecycle {
    ignore_changes = [template[0].container[0].image, template[0].revision_suffix, ingress[0].traffic_weight]
  }

  depends_on = [time_sleep.pull_role]
}

resource "azurerm_role_assignment" "deployer_apps" {
  for_each             = local.apps
  scope                = azurerm_container_app.quote[each.key].id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.deployer.principal_id
}

# What the scripts need to reach Azure, kept out of the public repository.
# Without the apps, the registry and addresses are empty: the scripts then
# say to run scripts/demo.sh up clouds.
resource "aws_ssm_parameter" "azure" {
  name        = "/release-demo/clouds/azure"
  type        = "String"
  description = "quote-service on Azure: tenant, subscription, deploy identity, registry, app addresses. Terraform, infra/demos/5-three-clouds/azure."
  value = jsonencode({
    tenant_id       = data.azurerm_client_config.current.tenant_id
    subscription_id = data.azurerm_client_config.current.subscription_id
    resource_group  = azurerm_resource_group.quote.name
    client_id       = azurerm_user_assigned_identity.deployer.client_id
    identity_name   = azurerm_user_assigned_identity.deployer.name
    registry        = var.apps ? azurerm_container_registry.quote[0].login_server : ""
    repository      = "quote-service"
    urls            = { for env, app in azurerm_container_app.quote : env => "https://${app.ingress[0].fqdn}" }
  })
}

output "quote_service_azure_urls" {
  value = { for env, app in azurerm_container_app.quote : env => "https://${app.ingress[0].fqdn}" }
}

# For up.sh: where seed.py copies 1.1.0 before the apps exist.
output "registry" {
  value = var.apps ? azurerm_container_registry.quote[0].login_server : ""
}

output "repository" {
  value = "quote-service"
}
