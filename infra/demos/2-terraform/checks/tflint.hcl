# tflint for the network stack: Terraform's recommended rules, plus the AWS
# rules that catch values AWS would refuse, such as an instance type that
# doesn't exist. The toolchain image (jenkins/toolchain/terraform.Dockerfile)
# installs the plugin at this version, so a run downloads nothing.

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "aws" {
  enabled = true
  version = "0.49.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}
