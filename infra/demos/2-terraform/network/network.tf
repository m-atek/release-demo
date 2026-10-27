# A small, real network for the store services: a VPC, two private subnets
# and one security group. No internet gateway, NAT gateway or instances, so it
# costs nothing.

resource "aws_vpc" "main" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "network-stack" }
}

resource "aws_subnet" "private" {
  for_each          = { a = "10.42.0.0/24", b = "10.42.1.0/24" }
  vpc_id            = aws_vpc.main.id
  cidr_block        = each.value
  availability_zone = "eu-west-1${each.key}"
  tags              = { Name = "network-stack-private-${each.key}" }
}

# The default group lets its members talk to each other. Empty it, so nothing
# relies on it by accident.
resource "aws_default_security_group" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "network-stack-default-unused" }
}

# The store services' group. Its rules are inline, so Terraform owns the whole
# list: a rule added by hand in the console shows up as drift, and the next
# apply removes it.
resource "aws_security_group" "app" {
  name        = "network-stack-app"
  description = "Store services: HTTPS inside the VPC only"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTPS from inside the VPC"
    protocol    = "tcp"
    from_port   = 443
    to_port     = 443
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  egress {
    description = "HTTPS to inside the VPC"
    protocol    = "tcp"
    from_port   = 443
    to_port     = 443
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  tags = { Name = "network-stack-app" }
}
