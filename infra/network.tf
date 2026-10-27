# A small VPC of its own: public subnets with an internet gateway and no NAT
# gateway (USD 35 a month if left on). The tools host calls out through its
# public address; nothing calls in.

resource "aws_vpc" "demo" {
  cidr_block           = "10.40.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = { Name = "release-demo" }
}

resource "aws_internet_gateway" "demo" {
  vpc_id = aws_vpc.demo.id
  tags   = { Name = "release-demo" }
}

resource "aws_subnet" "public" {
  for_each          = { a = "10.40.0.0/24", b = "10.40.1.0/24" }
  vpc_id            = aws_vpc.demo.id
  cidr_block        = each.value
  availability_zone = "${local.region}${each.key}"
  tags              = { Name = "release-demo-public-${each.key}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.demo.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.demo.id
  }
  tags = { Name = "release-demo-public" }
}

resource "aws_route_table_association" "public" {
  for_each       = aws_subnet.public
  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

# The VPC's default security group allows traffic between its members. Empty
# it, so nothing relies on it by accident.
resource "aws_default_security_group" "demo" {
  vpc_id = aws_vpc.demo.id
  tags   = { Name = "release-demo-default-unused" }
}
