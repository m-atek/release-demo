# Demo 19: Argo CD's UI on the cluster host, at argocd-demo.es.nl. The host
# itself is in infra/demos/19-argocd-hand-edit; this root gives it its fixed
# private address and puts the UI on the tools host's tunnel, behind its own
# Access application with the existing policy "release-demo: Mateusz". The
# tunnel's ingress rule is in cloudflare.tf, next to the others.
#
# Argo CD answers on the host's NodePort 30080 without TLS: Cloudflare ends
# TLS, and the host's security group lets in only the tools host, where
# cloudflared runs. Past Access, Argo CD is read-only for everyone.

locals {
  # The 200th address of the tools host's subnet: high, so AWS is unlikely to
  # have given it to another instance first (it picks addresses at random).
  cluster_host_address = cidrhost(aws_subnet.public["a"].cidr_block, 200)
  argocd_hostname      = "argocd-demo.es.nl"
  # The ingress rule cloudflare.tf adds before its catch-all.
  argocd_ingress = { hostname = local.argocd_hostname, service = "http://${local.cluster_host_address}:30080" }
}

resource "aws_ssm_parameter" "cluster_host_address" {
  name        = "/release-demo/cluster-host/address"
  type        = "String"
  value       = local.cluster_host_address
  description = substr(local.parameters["cluster-host/address"].help, 0, 1024)
}

resource "cloudflare_dns_record" "argocd" {
  zone_id = local.cf_zone
  name    = local.argocd_hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.tools_host.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "release-demo tools host tunnel, Argo CD on the cluster host. Managed by Terraform in release-demo."
}

resource "cloudflare_zero_trust_access_application" "argocd" {
  account_id           = local.cf_account
  name                 = "release-demo Argo CD"
  type                 = "self_hosted"
  domain               = local.argocd_hostname
  session_duration     = "24h"
  app_launcher_visible = false
  policies             = [{ id = cloudflare_zero_trust_access_policy.mateusz.id, precedence = 1 }]
}
