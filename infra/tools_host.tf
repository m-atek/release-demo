# The tools host: one t3.xlarge running Jenkins, JCR and Splunk in Docker,
# with the Jenkins agent on the host. No inbound rules at all; shell access
# through SSM Session Manager only; IMDSv2 with a hop limit of 1, so containers
# can't reach the instance role. All data lives on a separate 100 GB volume.
# It stops itself after 30 idle minutes; scripts/tools.sh starts it.

data "aws_ssm_parameter" "ubuntu_ami" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

data "aws_ssm_parameter" "github_repository" {
  name = "/release-demo/github/repository"
}

resource "aws_security_group" "tools_host" {
  name        = "release-demo-tools-host"
  description = "Tools host: no inbound. JCR 8082 opens later only to the security groups of demo servers."
  vpc_id      = aws_vpc.demo.id

  egress {
    description = "Calls out: package mirrors, GitHub, AWS APIs, Cloudflare, SaaS tools"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
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

resource "aws_iam_role" "tools_host" {
  name               = "release-demo-tools-host"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "tools_host_ssm" {
  role       = aws_iam_role.tools_host.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "tools_host" {
  statement {
    sid       = "ReadOwnParameters"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = ["arn:aws:ssm:${local.region}:${local.account_id}:parameter/release-demo/*"]
  }
  statement {
    sid     = "NotForTheHost"
    effect  = "Deny"
    actions = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = [for name in ["cloudflare/api-token", "servicenow/admin-password", "servicenow/sam-password", "servicenow/alex-password", "grafana/setup-token", "gitlab/api-token"] :
    "arn:aws:ssm:${local.region}:${local.account_id}:parameter/release-demo/${name}"]
  }
  statement {
    sid     = "WriteHostMadeTokens"
    actions = ["ssm:PutParameter"]
    resources = [
      "arn:aws:ssm:${local.region}:${local.account_id}:parameter/release-demo/jenkins/release-bot-api-token",
      "arn:aws:ssm:${local.region}:${local.account_id}:parameter/release-demo/jcr/token",
      # Demo 6: the Mac and Windows agents' Jenkins secrets (tools-host/agent_secrets.py).
      "arn:aws:ssm:${local.region}:${local.account_id}:parameter/release-demo/jenkins/agent-secret/*",
    ]
  }
  statement {
    sid       = "AssumeDeployRoles"
    actions   = ["sts:AssumeRole"]
    resources = ["arn:aws:iam::${local.account_id}:role/release-demo-deploy-*"]
  }
  # reset.py warms the Slack handler before a take; it needs only its address.
  statement {
    sid       = "FindTheSlackHandler"
    actions   = ["lambda:GetFunctionUrlConfig"]
    resources = [local.slack_handler_arn]
  }
}

resource "aws_iam_role_policy" "tools_host" {
  name   = "parameters-and-deploy-roles"
  role   = aws_iam_role.tools_host.id
  policy = data.aws_iam_policy_document.tools_host.json
}

resource "aws_iam_instance_profile" "tools_host" {
  name = "release-demo-tools-host"
  role = aws_iam_role.tools_host.name
}

resource "aws_instance" "tools_host" {
  ami                                  = data.aws_ssm_parameter.ubuntu_ami.value
  instance_type                        = "t3.xlarge"
  subnet_id                            = aws_subnet.public["a"].id
  associate_public_ip_address          = true
  vpc_security_group_ids               = [aws_security_group.tools_host.id]
  iam_instance_profile                 = aws_iam_instance_profile.tools_host.name
  instance_initiated_shutdown_behavior = "stop"

  user_data = templatefile("${path.module}/../tools-host/user-data.sh.tftpl", {
    data_volume_id = aws_ebs_volume.tools_data.id
    repository_url = "https://github.com/${nonsensitive(data.aws_ssm_parameter.github_repository.value)}.git"
  })

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  # 60 GB: Docker 29's containerd image store keeps image layers in
  # /var/lib/containerd on this disk, not in the data volume's data-root.
  root_block_device {
    volume_type = "gp3"
    volume_size = 60
    encrypted   = true
  }

  tags = { Name = "release-demo-tools-host", Role = "tools-host" }

  # New AMIs and boot changes don't replace the host: boot.sh comes from git
  # on every start.
  lifecycle {
    ignore_changes = [ami, user_data]
  }
}

resource "aws_ebs_volume" "tools_data" {
  availability_zone = aws_subnet.public["a"].availability_zone
  type              = "gp3"
  size              = 100
  encrypted         = true
  tags              = { Name = "release-demo-tools-data" }
}

resource "aws_volume_attachment" "tools_data" {
  device_name                    = "/dev/sdf"
  volume_id                      = aws_ebs_volume.tools_data.id
  instance_id                    = aws_instance.tools_host.id
  stop_instance_before_detaching = true
}

output "tools_host_instance_id" {
  value = aws_instance.tools_host.id
}
