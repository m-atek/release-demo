output "vpc_id" {
  value = aws_vpc.main.id
}

output "subnet_ids" {
  value = [for subnet in aws_subnet.private : subnet.id]
}

output "security_group_id" {
  value = aws_security_group.app.id
}
