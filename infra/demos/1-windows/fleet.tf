# The fleet: t3.medium instances from AWS's current Windows Server 2022 base
# image, which includes the SSM Agent and AWS Tools for PowerShell. No custom
# image. No inbound rules, no RDP, no WinRM, no key pair: every step runs
# through SSM Run Command. IMDSv2 only. Each server stops itself 4 hours after
# boot, so a forgotten fleet costs only its disks.

data "aws_ssm_parameter" "windows_ami" {
  name = "/aws/service/ami-windows-latest/Windows_Server-2022-English-Full-Base"
}

resource "aws_security_group" "fleet" {
  name        = "release-demo-store-fleet"
  description = "Store fleet: no inbound. Out: AWS APIs on 443, JCR on the tools host on 8082."
  vpc_id      = data.aws_vpc.demo.id
  tags        = { Name = "release-demo-store-fleet" }
}

resource "aws_vpc_security_group_egress_rule" "fleet_https" {
  security_group_id = aws_security_group.fleet.id
  description       = "SSM, EC2 messages and SSM Parameter Store over HTTPS"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "fleet_jcr" {
  security_group_id            = aws_security_group.fleet.id
  description                  = "JFrog Container Registry on the tools host"
  ip_protocol                  = "tcp"
  from_port                    = 8082
  to_port                      = 8082
  referenced_security_group_id = data.aws_security_group.tools_host.id
}

# The one change to the shared platform: JCR on the tools host accepts the
# fleet, and only the fleet, on 8082. Removed with this root.
resource "aws_vpc_security_group_ingress_rule" "tools_host_jcr_from_fleet" {
  security_group_id            = data.aws_security_group.tools_host.id
  description                  = "JCR for the store fleet (demo 1)"
  ip_protocol                  = "tcp"
  from_port                    = 8082
  to_port                      = 8082
  referenced_security_group_id = aws_security_group.fleet.id
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "fleet" {
  name               = "release-demo-store-fleet"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "fleet_ssm" {
  role       = aws_iam_role.fleet.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# One parameter: the read-only JCR token. Nothing else in SSM.
data "aws_iam_policy_document" "fleet" {
  statement {
    sid       = "ReadTheJcrReadToken"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.token.arn]
  }
}

resource "aws_iam_role_policy" "fleet" {
  name   = "read-jcr-token"
  role   = aws_iam_role.fleet.id
  policy = data.aws_iam_policy_document.fleet.json
}

resource "aws_iam_instance_profile" "fleet" {
  name = "release-demo-store-fleet"
  role = aws_iam_role.fleet.name
}

resource "aws_instance" "store" {
  for_each                             = local.servers
  ami                                  = nonsensitive(data.aws_ssm_parameter.windows_ami.value)
  instance_type                        = "t3.medium"
  subnet_id                            = data.aws_subnet.public[each.value.subnet].id
  associate_public_ip_address          = true
  vpc_security_group_ids               = [aws_security_group.fleet.id]
  iam_instance_profile                 = aws_iam_instance_profile.fleet.name
  instance_initiated_shutdown_behavior = "stop"
  user_data                            = file("${path.module}/user-data.ps1")

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 30
    encrypted   = true
  }

  tags = { Name = each.key, Role = "store-server", Store = each.value.store }

  # A newer base image doesn't replace a running fleet; the next up after a
  # down builds from the current one.
  lifecycle {
    ignore_changes = [ami, user_data]
  }
}
