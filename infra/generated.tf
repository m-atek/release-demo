# Passwords and tokens nobody types: generated here and stored in SSM, where
# the tools host reads them at boot. Letters and digits only, so they survive
# env files and JCasC unquoted. They also sit in the encrypted state.

resource "random_password" "generated" {
  for_each = toset(["jenkins/admin-password", "jenkins/release-bot-password", "jcr/db-password", "splunk/password"])
  length   = 32
  special  = false
}

resource "random_uuid" "splunk_hec_token" {}

locals {
  generated = merge(
    { for name, p in random_password.generated : name => p.result },
    { "splunk/hec-token" = random_uuid.splunk_hec_token.result },
  )
}

resource "aws_ssm_parameter" "generated" {
  for_each    = local.generated
  name        = "/release-demo/${each.key}"
  type        = "SecureString"
  value       = each.value
  description = substr(local.parameters[each.key].help, 0, 1024)
}
