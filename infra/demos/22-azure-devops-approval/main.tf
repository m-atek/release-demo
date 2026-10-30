# Demo 22, Azure DevOps: Release Bot, which the Slack handler queues runs as
# and the tools host asks Azure DevOps as, with no stored secret.
#
# On the Entra side, in the es.nl tenant: the app registration "release-demo
# Release Bot" with no API permissions and no secret, its service principal,
# and two federated credentials. Each trusts one AWS role, the Slack handler's
# and the tools host's, through AWS's outbound identity federation: the role
# gets a five-minute token AWS signs (sts:GetWebIdentityToken) and Entra
# exchanges it for an Azure DevOps token. Azure DevOps gives Release Bot its
# rights (scripts/demos/azure_devops/setup.py): queue the release pipeline,
# read and cancel its runs, nothing else.
#
# On the AWS side, in the demo's own account: the account setting that turns
# outbound identity federation on, and the two roles' leave to ask for such a
# token, for Entra's audience only. Nothing else in the tenant or the account
# changes. Its own root and state; run it through
# scripts/demos/azure_devops/tf.sh, signed in with az login and to AWS.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.69.0" }
    azuread = { source = "hashicorp/azuread", version = "~> 3.10.0" }
  }
  # The bucket comes from SSM at init: scripts/demos/azure_devops/tf.sh init.
  backend "s3" {
    key          = "release-demo/demos/22-azure-devops-approval.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "22-azure-devops-approval", ManagedBy = "terraform" }
  }
}

# Your az login, in the es.nl tenant.
provider "azuread" {}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "azuread_client_config" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  parameters = yamldecode(file("${path.module}/../../parameters.yaml"))
  # The roles Release Bot trusts, made by infra/: the Slack handler's and the tools host's.
  trusted_roles = {
    slack-handler = "release-demo-slack-handler"
    tools-host    = "release-demo-tools-host"
  }
  token_exchange = "api://AzureADTokenExchange"
}

# AWS: an account-wide setting. Destroying this resource turns outbound
# identity federation off for the whole demo account.
resource "aws_iam_outbound_web_identity_federation" "account" {}

resource "azuread_application" "release_bot" {
  display_name     = "release-demo Release Bot"
  description      = "Queues the release-demo release pipeline in Azure DevOps, for Slack's /release. No secret: AWS roles sign in through federated credentials."
  sign_in_audience = "AzureADMyOrg"
  owners           = [data.azuread_client_config.current.object_id]
  tags             = ["release-demo", "Demo=22-azure-devops-approval"]
}

resource "azuread_service_principal" "release_bot" {
  client_id = azuread_application.release_bot.client_id
  owners    = [data.azuread_client_config.current.object_id]
  tags      = ["release-demo", "Demo=22-azure-devops-approval"]
}

resource "azuread_application_federated_identity_credential" "release_bot" {
  for_each       = local.trusted_roles
  application_id = azuread_application.release_bot.id
  display_name   = "aws-${each.key}"
  description    = "The AWS role ${each.value}, through AWS's outbound identity federation."
  issuer         = aws_iam_outbound_web_identity_federation.account.issuer_identifier
  subject        = "arn:aws:iam::${local.account_id}:role/${each.value}"
  audiences      = [local.token_exchange]
}

# Each trusted role may ask AWS for a token for Entra, and for nothing else.
data "aws_iam_policy_document" "release_bot" {
  for_each = local.trusted_roles

  statement {
    sid       = "TokenForEntra"
    actions   = ["sts:GetWebIdentityToken"]
    resources = ["*"]
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "sts:IdentityTokenAudience"
      values   = [local.token_exchange]
    }
    condition {
      test     = "NumericLessThanEquals"
      variable = "sts:DurationSeconds"
      values   = ["300"]
    }
  }
  # The Slack handler reads only its own parameters: the names of the organization,
  # the pipeline, the tenant and Release Bot. The tools host reads them already.
  dynamic "statement" {
    for_each = each.key == "slack-handler" ? [1] : []
    content {
      sid       = "ReadAzureDevOpsNames"
      actions   = ["ssm:GetParameter", "ssm:GetParameters"]
      resources = ["arn:aws:ssm:${local.region}:${local.account_id}:parameter/release-demo/azure-devops/*"]
    }
  }
}

resource "aws_iam_role_policy" "release_bot" {
  for_each = local.trusted_roles
  name     = "azure-devops-release-bot"
  role     = each.value
  policy   = data.aws_iam_policy_document.release_bot[each.key].json
}

# What the Slack handler and the tools host need to ask for Release Bot.
resource "aws_ssm_parameter" "release_bot" {
  for_each = {
    "azure-devops/tenant-id"             = data.azuread_client_config.current.tenant_id
    "azure-devops/release-bot-client-id" = azuread_application.release_bot.client_id
  }
  name        = "/release-demo/${each.key}"
  type        = "String"
  value       = each.value
  description = substr(local.parameters[each.key].help, 0, 1024)
}

output "release_bot" {
  description = "Release Bot's ids, for Azure DevOps' Users page: add it with Basic access (setup.py does)."
  value = {
    client_id    = azuread_application.release_bot.client_id
    object_id    = azuread_service_principal.release_bot.object_id
    display_name = azuread_application.release_bot.display_name
  }
}
