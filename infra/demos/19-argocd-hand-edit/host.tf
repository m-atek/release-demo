# The cluster host: t3.large (2 vCPUs, 8 GB), Ubuntu 24.04 LTS, 20 GB gp3, in
# the tools host's subnet at the fixed address infra/ chose. A public address
# for outbound calls only (GitHub, the k3s and Argo CD images), IMDSv2 with a
# hop limit of 1 so pods can't reach the instance role, and shutdown
# behaviour stop: the idle stop powers it off and the next up starts it with
# the same cluster and Argo CD state.

resource "aws_security_group" "cluster_host" {
  name        = "release-demo-cluster-host"
  description = "Cluster host: Argo CD on 30080 from the tools host only. Out to GitHub, image registries and JCR."
  vpc_id      = data.aws_vpc.demo.id

  egress {
    description = "Calls out: GitHub, k3s and Argo CD images, AWS APIs, JCR on the tools host"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "release-demo-cluster-host" }
}

resource "aws_vpc_security_group_ingress_rule" "argocd_from_tools_host" {
  security_group_id            = aws_security_group.cluster_host.id
  description                  = "Argo CD for cloudflared, the fix pipeline and reset.py on the tools host"
  ip_protocol                  = "tcp"
  from_port                    = 30080
  to_port                      = 30080
  referenced_security_group_id = data.aws_security_group.tools_host.id
  tags                         = { Name = "release-demo-argocd-from-tools-host" }
}

# The one change to the platform: JCR on the tools host accepts this host's pulls.
resource "aws_vpc_security_group_ingress_rule" "jcr_from_cluster_host" {
  security_group_id            = data.aws_security_group.tools_host.id
  description                  = "JCR for the cluster host image pulls (demo 19)"
  ip_protocol                  = "tcp"
  from_port                    = 8082
  to_port                      = 8082
  referenced_security_group_id = aws_security_group.cluster_host.id
  tags                         = { Name = "release-demo-jcr-from-cluster-host" }
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

resource "aws_iam_role" "cluster_host" {
  name               = "release-demo-cluster-host"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "cluster_host_ssm" {
  role       = aws_iam_role.cluster_host.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Its own settings, the recorder's token for the bridge, and the read-only JCR
# token demo 3 made; it writes only the Argo CD token its setup generates.
data "aws_iam_policy_document" "cluster_host" {
  statement {
    sid       = "ReadOwnParameters"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = [for name in local.reads : "arn:aws:ssm:${local.region}:${local.account_id}:parameter/release-demo/${name}"]
  }
  statement {
    sid       = "StoreThePipelineToken"
    actions   = ["ssm:PutParameter"]
    resources = [for name in local.writes : "arn:aws:ssm:${local.region}:${local.account_id}:parameter/release-demo/${name}"]
  }
}

resource "aws_iam_role_policy" "cluster_host" {
  name   = "own-parameters"
  role   = aws_iam_role.cluster_host.id
  policy = data.aws_iam_policy_document.cluster_host.json
}

resource "aws_iam_instance_profile" "cluster_host" {
  name = "release-demo-cluster-host"
  role = aws_iam_role.cluster_host.name
}

# What the boot reads: the repository Argo CD syncs from, and JCR on the tools
# host's private address, which k3s mirrors artifactory-demo.es.nl to.
resource "aws_ssm_parameter" "settings" {
  name = "/release-demo/cluster-host/settings"
  type = "String"
  value = jsonencode({
    repository = nonsensitive(data.aws_ssm_parameter.platform["github/repository"].value)
    address    = nonsensitive(data.aws_ssm_parameter.platform["cluster-host/address"].value)
    bridge_url = nonsensitive(data.aws_ssm_parameter.platform["bridge/url"].value)
    jcr        = "http://${data.aws_instance.tools_host.private_ip}:8082"
  })
  description = substr(local.parameters["cluster-host/settings"].help, 0, 1024)
}

resource "aws_instance" "cluster_host" {
  ami                                  = data.aws_ssm_parameter.ubuntu_ami.value
  instance_type                        = "t3.large"
  subnet_id                            = data.aws_subnet.a.id
  private_ip                           = nonsensitive(data.aws_ssm_parameter.platform["cluster-host/address"].value)
  associate_public_ip_address          = true
  vpc_security_group_ids               = [aws_security_group.cluster_host.id]
  iam_instance_profile                 = aws_iam_instance_profile.cluster_host.name
  instance_initiated_shutdown_behavior = "stop"

  user_data = templatefile("${path.module}/user-data.sh.tftpl", {
    repository_url = "https://github.com/${nonsensitive(data.aws_ssm_parameter.platform["github/repository"].value)}.git"
  })

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 20
    encrypted   = true
  }

  tags = { Name = "release-demo-cluster-host", Role = "cluster-host" }

  # New AMIs and boot changes don't replace the host: its boot script comes
  # from git on every start. Nor does the public address a stopped host gives up.
  lifecycle {
    ignore_changes = [ami, user_data, associate_public_ip_address]
  }

  depends_on = [aws_ssm_parameter.settings]
}

output "cluster_host_instance_id" {
  value = aws_instance.cluster_host.id
}
