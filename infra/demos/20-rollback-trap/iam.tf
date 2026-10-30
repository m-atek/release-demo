# Who may read which connection string, and the rollback check's call.
#
# - Each function's role reads only its own environment's database-url
#   (pooled, as orders_app), at cold start.
# - The deploy role, which the tools host assumes for staging and production,
#   reads the two migrate-urls (direct, as orders_owner) to run migrations,
#   and may invoke orders-service-staging's published versions: the rollback
#   check runs production's version there, against staging's database.
# - The tools host's own role reads /release-demo/* already (infra/tools_host.tf).

data "aws_iam_role" "function" {
  for_each = local.orders.environments
  name     = "${each.value.function}-function"
}

data "aws_iam_role" "deploy" {
  name = "release-demo-deploy-orders-service"
}

data "aws_iam_policy_document" "function_database_url" {
  for_each = local.orders.environments
  statement {
    sid       = "ReadOwnDatabaseUrl"
    actions   = ["ssm:GetParameter"]
    resources = ["${local.parameter}/${each.key}/database-url"]
  }
}

resource "aws_iam_role_policy" "function_database_url" {
  for_each = local.orders.environments
  name     = "read-own-database-url"
  role     = data.aws_iam_role.function[each.key].name
  policy   = data.aws_iam_policy_document.function_database_url[each.key].json
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "ReadMigrateUrls"
    actions   = ["ssm:GetParameter"]
    resources = [for env in keys(local.orders.environments) : "${local.parameter}/${env}/migrate-url"]
  }
  statement {
    sid     = "RollbackCheckInvokesStagingVersions"
    actions = ["lambda:InvokeFunction"]
    resources = [
      "arn:aws:lambda:${local.region}:${local.account_id}:function:${local.orders.environments.staging.function}:*",
    ]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "rollback-trap-migrations-and-rollback-check"
  role   = data.aws_iam_role.deploy.name
  policy = data.aws_iam_policy_document.deploy.json
}

output "policies" {
  description = "The inline policies this root adds, by role."
  value = merge(
    { for env, role in data.aws_iam_role.function : role.name => aws_iam_role_policy.function_database_url[env].name },
    { (data.aws_iam_role.deploy.name) = aws_iam_role_policy.deploy.name },
  )
}
