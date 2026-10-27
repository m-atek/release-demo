output "fleet" {
  description = "Instance id of each store server."
  value       = { for name, instance in aws_instance.store : name => instance.id }
}

output "deploy_role_arn" {
  value = aws_iam_role.deploy.arn
}
