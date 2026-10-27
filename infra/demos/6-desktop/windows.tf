# The Windows track: a build agent that connects out to Jenkins over
# WebSocket, and a test instance the pipeline installs the MSI on through
# Run Command. Windows Server 2022, t3.medium, in the platform's public
# subnet, with no inbound rules and no RDP. Each stops itself 4 hours after
# every boot. scripts/demos/desktop/up.sh starts them, down.sh stops them.

data "aws_ssm_parameter" "windows_ami" {
  name = "/aws/service/ami-windows-latest/Windows_Server-2022-English-Full-Base"
}

data "aws_subnet" "public" {
  filter {
    name   = "tag:Name"
    values = ["release-demo-public-a"]
  }
}

resource "aws_security_group" "windows" {
  name        = "release-demo-desktop-windows"
  description = "Demo 6 Windows instances: no inbound, calls out only"
  vpc_id      = data.aws_subnet.public.vpc_id

  egress {
    description = "Jenkins through Cloudflare, AWS APIs, NuGet, package downloads"
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

locals {
  windows_roles = {
    build = {
      name = "release-demo-desktop-build"
      # The agent's Cloudflare Access service token and its Jenkins secret.
      parameters = ["cloudflare/windows-agent-client-id", "cloudflare/windows-agent-client-secret", "jenkins/agent-secret/windows"]
      # A build outside Jenkins (scripts/demos/desktop/windows_try.py): its code in, its parts out.
      objects = ["source/*"]
      writes  = ["tryout/*"]
      volume  = 50
    }
    test = {
      name       = "release-demo-desktop-test"
      parameters = []
      objects    = ["installers/*"]
      writes     = []
      volume     = 30
    }
  }
}

resource "aws_iam_role" "windows" {
  for_each           = local.windows_roles
  name               = each.value.name
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "windows_ssm" {
  for_each   = local.windows_roles
  role       = aws_iam_role.windows[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "windows" {
  for_each = local.windows_roles
  statement {
    actions   = ["s3:GetObject"]
    resources = [for prefix in each.value.objects : "${aws_s3_bucket.desktop.arn}/${prefix}"]
  }
  dynamic "statement" {
    for_each = length(each.value.writes) > 0 ? [1] : []
    content {
      actions   = ["s3:PutObject"]
      resources = [for prefix in each.value.writes : "${aws_s3_bucket.desktop.arn}/${prefix}"]
    }
  }
  dynamic "statement" {
    for_each = length(each.value.parameters) > 0 ? [1] : []
    content {
      actions   = ["ssm:GetParameter"]
      resources = [for name in each.value.parameters : "arn:aws:ssm:${local.region}:${local.account_id}:parameter/release-demo/${name}"]
    }
  }
}

resource "aws_iam_role_policy" "windows" {
  for_each = local.windows_roles
  name     = "own-objects-and-parameters"
  role     = aws_iam_role.windows[each.key].id
  policy   = data.aws_iam_policy_document.windows[each.key].json
}

resource "aws_iam_instance_profile" "windows" {
  for_each = local.windows_roles
  name     = each.value.name
  role     = aws_iam_role.windows[each.key].name
}

resource "aws_instance" "windows" {
  for_each                             = var.windows ? local.windows_roles : {}
  ami                                  = data.aws_ssm_parameter.windows_ami.value
  instance_type                        = "t3.medium"
  subnet_id                            = data.aws_subnet.public.id
  associate_public_ip_address          = true
  vpc_security_group_ids               = [aws_security_group.windows.id]
  iam_instance_profile                 = aws_iam_instance_profile.windows[each.key].name
  instance_initiated_shutdown_behavior = "stop"
  user_data = templatefile("${path.module}/windows-${each.key}.ps1.tftpl", {
    jenkins_url = "https://jenkins-demo.es.nl/"
  })

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = each.value.volume
    encrypted   = true
  }

  tags = { Name = each.value.name, Role = "desktop-${each.key}" }

  # A new base image doesn't replace a set-up instance.
  lifecycle {
    ignore_changes = [ami]
  }
}

output "windows_instances" {
  value = { for key, instance in aws_instance.windows : key => instance.id }
}
