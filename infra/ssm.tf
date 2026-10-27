# The names of the parameters others fill in, from parameters.yaml. Terraform
# writes "unset" once and ignores the value after that.

resource "aws_ssm_parameter" "placeholder" {
  for_each    = local.placeholders
  name        = "/release-demo/${each.key}"
  type        = each.value.secure ? "SecureString" : "String"
  value       = "unset"
  description = substr(each.value.help, 0, 1024)

  lifecycle {
    ignore_changes = [value]
  }
}
