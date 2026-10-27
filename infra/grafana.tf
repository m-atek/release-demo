# Grafana Assume Role: Grafana Cloud reads Lambda metrics from CloudWatch
# through this role, so Grafana holds no AWS keys. Grafana's own AWS account id
# is shown in the CloudWatch data source's Settings tab; set it in
# infra/local.auto.tfvars. Until then the role isn't created.

variable "grafana_aws_account_id" {
  description = "Grafana Cloud's AWS account, from the CloudWatch data source's Grafana Assume Role instructions."
  type        = string
  default     = ""
}

data "aws_ssm_parameter" "grafana_external_id" {
  count = var.grafana_aws_account_id == "" ? 0 : 1
  name  = "/release-demo/grafana/external-id"
}

data "aws_iam_policy_document" "grafana_trust" {
  count = var.grafana_aws_account_id == "" ? 0 : 1
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${var.grafana_aws_account_id}:root"]
    }
    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = [data.aws_ssm_parameter.grafana_external_id[0].value]
    }
  }
}

# Read-only metrics: what the CloudWatch data source needs for Lambda panels.
data "aws_iam_policy_document" "grafana_cloudwatch" {
  statement {
    actions = [
      "cloudwatch:GetMetricData",
      "cloudwatch:GetMetricStatistics",
      "cloudwatch:ListMetrics",
      "cloudwatch:DescribeAlarms",
      "cloudwatch:DescribeAlarmsForMetric",
      "cloudwatch:DescribeAlarmHistory",
      "ec2:DescribeRegions",
      "tag:GetResources",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role" "grafana" {
  count              = var.grafana_aws_account_id == "" ? 0 : 1
  name               = "release-demo-grafana-cloudwatch"
  assume_role_policy = data.aws_iam_policy_document.grafana_trust[0].json
}

resource "aws_iam_role_policy" "grafana" {
  count  = var.grafana_aws_account_id == "" ? 0 : 1
  name   = "cloudwatch-read"
  role   = aws_iam_role.grafana[0].id
  policy = data.aws_iam_policy_document.grafana_cloudwatch.json
}

output "grafana_role_arn" {
  description = "Paste into the CloudWatch data source, Grafana Assume Role, Assume Role ARN."
  value       = one(aws_iam_role.grafana[*].arn)
}
