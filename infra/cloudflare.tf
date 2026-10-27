# Cloudflare: the only way in to the tools host. A tunnel that cloudflared on
# the host dials out to, three hostnames on it, and a Cloudflare Access
# application in front of each: Mateusz's email, plus a service token for the
# Slack handler on Jenkins. Splunk Free has no login of its own, so Access is
# what keeps it private.
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
  )
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "tools_host" {
  account_id = local.cf_account
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.tools_host.id
}

# The host and the Slack handler read these from SSM.
resource "aws_ssm_parameter" "cloudflare_generated" {
  for_each = {
    "cloudflare/tunnel-token"                = data.cloudflare_zero_trust_tunnel_cloudflared_token.tools_host.token
    "cloudflare/slack-handler-client-id"     = cloudflare_zero_trust_access_service_token.slack_handler.client_id
    "cloudflare/slack-handler-client-secret" = cloudflare_zero_trust_access_service_token.slack_handler.client_secret
  }
  name        = "/release-demo/${each.key}"
  type        = "SecureString"
  value       = each.value
  description = substr(local.parameters[each.key].help, 0, 1024)
}
