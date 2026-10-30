# Cloudflare: the only way in to the tools host. A tunnel that cloudflared on
# the host dials out to, three hostnames on it, and a Cloudflare Access
# application in front of each: Mateusz's email, plus a service token for the
# Slack handler on Jenkins, and one for each Jenkins agent outside the tools
# host. Splunk Free has no login of its own, so Access is what keeps it private.
#
# es.nl also serves the production website. This file only adds its own,
# separately named resources and never imports or changes anything else in
# the zone.

provider "cloudflare" {}

data "aws_ssm_parameter" "cloudflare" {
  for_each = toset(["account-id", "zone-id", "access-email"])
  name     = "/release-demo/cloudflare/${each.key}"
}

locals {
  cf_account = data.aws_ssm_parameter.cloudflare["account-id"].value
  cf_zone    = data.aws_ssm_parameter.cloudflare["zone-id"].value
  # Hostname, the service behind it on the tools host's Docker network, and the Access app's name.
  tool_hostnames = {
    jenkins     = { hostname = "jenkins-demo.es.nl", service = "http://jenkins:8080", name = "release-demo Jenkins" }
    artifactory = { hostname = "artifactory-demo.es.nl", service = "http://jcr:8082", name = "release-demo JFrog Container Registry" }
    splunk      = { hostname = "splunk-demo.es.nl", service = "http://splunk:8000", name = "release-demo Splunk" }
  }
  # Jenkins agents that connect from outside, each with its own service token
  # and the Access apps it may reach: demo 3's data-center agent (Jenkins, and
  # JCR for its packages) and demo 6's Mac and Windows build agents.
  # The key names their SSM values: cloudflare/<key>-client-id and -client-secret.
  remote_agents = {
    dc-agent      = { name = "release-demo dc-agent", apps = ["jenkins", "artifactory"] }
    mac-agent     = { name = "release-demo Mac agent", apps = ["jenkins"] }
    windows-agent = { name = "release-demo Windows agent", apps = ["jenkins"] }
  }
  agents_by_app = { for app in keys(local.tool_hostnames) : app => sort([for key, agent in local.remote_agents : key if contains(agent.apps, app)]) }
}

resource "random_bytes" "tunnel_secret" {
  length = 32
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "tools_host" {
  account_id    = local.cf_account
  name          = "release-demo-tools-host"
  config_src    = "cloudflare"
  tunnel_secret = random_bytes.tunnel_secret.base64
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "tools_host" {
  account_id = local.cf_account
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.tools_host.id
  config = {
    ingress = concat(
      [for tool in local.tool_hostnames : { hostname = tool.hostname, service = tool.service }],
      # Demo 19: Argo CD on the cluster host's private address (infra/argocd.tf).
      [local.argocd_ingress],
      [{ service = "http_status:404" }],
    )
  }
}

resource "cloudflare_dns_record" "tools" {
  for_each = local.tool_hostnames
  zone_id  = local.cf_zone
  name     = each.value.hostname
  type     = "CNAME"
  content  = "${cloudflare_zero_trust_tunnel_cloudflared.tools_host.id}.cfargotunnel.com"
  proxied  = true
  ttl      = 1
  comment  = "release-demo tools host tunnel. Managed by Terraform in release-demo."
}

resource "cloudflare_zero_trust_access_service_token" "slack_handler" {
  account_id = local.cf_account
  name       = "release-demo Slack handler"
  duration   = "8760h"
}

resource "cloudflare_zero_trust_access_policy" "mateusz" {
  account_id       = local.cf_account
  name             = "release-demo: Mateusz"
  decision         = "allow"
  session_duration = "24h"
  include          = [{ email = { email = data.aws_ssm_parameter.cloudflare["access-email"].value } }]
}

resource "cloudflare_zero_trust_access_policy" "slack_handler" {
  account_id = local.cf_account
  name       = "release-demo: Slack handler"
  decision   = "non_identity"
  include    = [{ service_token = { token_id = cloudflare_zero_trust_access_service_token.slack_handler.id } }]
}

resource "cloudflare_zero_trust_access_service_token" "agents" {
  for_each   = local.remote_agents
  account_id = local.cf_account
  name       = each.value.name
  duration   = "8760h"
}

resource "cloudflare_zero_trust_access_policy" "agents" {
  for_each   = local.remote_agents
  account_id = local.cf_account
  name       = "release-demo: ${each.key}"
  decision   = "non_identity"
  include    = [{ service_token = { token_id = cloudflare_zero_trust_access_service_token.agents[each.key].id } }]
}

resource "cloudflare_zero_trust_access_application" "tools" {
  for_each             = local.tool_hostnames
  account_id           = local.cf_account
  name                 = each.value.name
  type                 = "self_hosted"
  domain               = each.value.hostname
  session_duration     = "24h"
  app_launcher_visible = false
  policies = concat(
    [{ id = cloudflare_zero_trust_access_policy.mateusz.id, precedence = 1 }],
    each.key == "jenkins" ? [{ id = cloudflare_zero_trust_access_policy.slack_handler.id, precedence = 2 }] : [],
    # The agents after both, so the existing two keep their places.
    [for i, agent in local.agents_by_app[each.key] :
    { id = cloudflare_zero_trust_access_policy.agents[agent].id, precedence = (each.key == "jenkins" ? 3 : 2) + i }],
  )
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "tools_host" {
  account_id = local.cf_account
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.tools_host.id
}

# The host, the Slack handler and the agents read these from SSM.
resource "aws_ssm_parameter" "cloudflare_generated" {
  for_each = merge({
    "cloudflare/tunnel-token"                = data.cloudflare_zero_trust_tunnel_cloudflared_token.tools_host.token
    "cloudflare/slack-handler-client-id"     = cloudflare_zero_trust_access_service_token.slack_handler.client_id
    "cloudflare/slack-handler-client-secret" = cloudflare_zero_trust_access_service_token.slack_handler.client_secret
    }, merge([for key, token in cloudflare_zero_trust_access_service_token.agents : {
      "cloudflare/${key}-client-id"     = token.client_id
      "cloudflare/${key}-client-secret" = token.client_secret
  }]...))
  name        = "/release-demo/${each.key}"
  type        = "SecureString"
  value       = each.value
  description = substr(local.parameters[each.key].help, 0, 1024)
}
