# inv-cloud-01 and inv-cloud-02: t3.small, Ubuntu 24.04 LTS, a public
# address for outbound calls only, no inbound rules, IMDSv2, and a timer that
# powers them off 4 hours after every boot (shutdown behaviour: stop).

resource "aws_security_group" "servers" {
  name        = "release-demo-inventory-servers"
  description = "inventory cloud servers: no inbound. Out to SSM, S3, package mirrors and JCR on the tools host."
  vpc_id      = data.aws_vpc.demo.id

  egress {
    description = "Calls out: Systems Manager, S3, Ubuntu and Apache mirrors, JCR on the tools host"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "release-demo-inventory-servers" }
}

# The one change to the platform: JCR on the tools host accepts these servers.
resource "aws_vpc_security_group_ingress_rule" "jcr_from_servers" {
  security_group_id            = data.aws_security_group.tools_host.id
  description                  = "JCR for the inventory cloud servers (demo 3)"
  ip_protocol                  = "tcp"
  from_port                    = 8082
  to_port                      = 8082
  referenced_security_group_id = aws_security_group.servers.id
  tags                         = { Name = "release-demo-jcr-from-inventory-servers" }
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

resource "aws_iam_role" "servers" {
  name               = "release-demo-inventory-server"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "servers_ssm" {
  role       = aws_iam_role.servers.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Each server reads the read-only JCR token and the app's secret, nothing else.
data "aws_iam_policy_document" "servers" {
  statement {
    sid     = "ReadOwnSecrets"
    actions = ["ssm:GetParameter"]
    resources = [for name in ["jcr/inventory-read-token", "inventory/app-secret"] :
    "arn:aws:ssm:${local.region}:${local.account_id}:parameter/release-demo/${name}"]
  }
}

resource "aws_iam_role_policy" "servers" {
  name   = "read-inventory-secrets"
  role   = aws_iam_role.servers.id
  policy = data.aws_iam_policy_document.servers.json
}

resource "aws_iam_instance_profile" "servers" {
  name = "release-demo-inventory-server"
  role = aws_iam_role.servers.name
}

resource "aws_instance" "server" {
  for_each                             = local.cloud_servers
  ami                                  = data.aws_ssm_parameter.ubuntu_ami.value
  instance_type                        = "t3.small"
  subnet_id                            = data.aws_subnet.public[each.value.subnet].id
  associate_public_ip_address          = true
  vpc_security_group_ids               = [aws_security_group.servers.id]
  iam_instance_profile                 = aws_iam_instance_profile.servers.name
  instance_initiated_shutdown_behavior = "stop"

  user_data = templatefile("${path.module}/user-data.yaml.tftpl", { hostname = each.key })

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 8
    encrypted   = true
  }

  tags = { Name = each.key, Role = "inventory-server", Service = "inventory" }

  # A new AMI doesn't replace a server: Ansible keeps them current.
  lifecycle {
    ignore_changes = [ami, user_data]
  }
}

output "servers" {
  description = "The cloud servers' instance ids."
  value       = { for name, server in aws_instance.server : name => server.id }
}
