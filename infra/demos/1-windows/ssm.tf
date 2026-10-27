# The fleet's read-only JCR token: Terraform creates the name with "unset" and
# never touches the value; scripts/demos/windows/jcr_token.py writes a token
# for a JCR user that only reads (the readers group), on the tools host.

resource "aws_ssm_parameter" "token" {
  name        = "/release-demo/${local.token_parameter}"
  type        = "SecureString"
  value       = "unset"
  description = "Read-only JCR token the store fleet downloads store-sync with. Written by scripts/demos/windows/jcr_token.py."

  lifecycle {
    ignore_changes = [value]
  }
}
