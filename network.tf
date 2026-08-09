data "aws_vpc" "default" {
  default = true
}

# The default VPC's main route table already routes 0.0.0.0/0 to the Internet Gateway.
# The proxy subnet reuses it - this is the only server allowed to reach the internet.
data "aws_route_table" "default" {
  vpc_id = data.aws_vpc.default.id

  filter {
    name   = "association.main"
    values = ["true"]
  }
}

resource "aws_subnet" "proxy" {
  vpc_id                  = data.aws_vpc.default.id
  cidr_block              = var.proxy_subnet_cidr
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = true

  tags = {
    Name = "${var.name}-proxy-subnet"
  }
}

resource "aws_route_table_association" "proxy" {
  subnet_id      = aws_subnet.proxy.id
  route_table_id = data.aws_route_table.default.id
}

# The AI subnet gets its own dedicated route table containing only the implicit local
# VPC route - no aws_route to an Internet Gateway is ever created here. This is a
# network-layer isolation guarantee: the AI instance cannot reach the internet no
# matter what its security group allows, because there is no route out for it to use.
resource "aws_route_table" "ai" {
  vpc_id = data.aws_vpc.default.id

  tags = {
    Name = "${var.name}-ai-rt"
  }
}

resource "aws_subnet" "ai" {
  vpc_id                  = data.aws_vpc.default.id
  cidr_block              = var.ai_subnet_cidr
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = false

  tags = {
    Name = "${var.name}-ai-subnet"
  }
}

resource "aws_route_table_association" "ai" {
  subnet_id      = aws_subnet.ai.id
  route_table_id = aws_route_table.ai.id
}

# The deploy subnet gets the same treatment as the AI subnet: its own dedicated route
# table with only the implicit local VPC route, no aws_route to an Internet Gateway.
# This instance serves HTTP to the internet, but only ever via the proxy reverse-
# proxying inbound traffic to it - it never gets a direct route out (or in) itself.
resource "aws_route_table" "deploy" {
  vpc_id = data.aws_vpc.default.id

  tags = {
    Name = "${var.name}-deploy-rt"
  }
}

resource "aws_subnet" "deploy" {
  vpc_id                  = data.aws_vpc.default.id
  cidr_block              = var.deploy_subnet_cidr
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = false

  tags = {
    Name = "${var.name}-deploy-subnet"
  }
}

resource "aws_route_table_association" "deploy" {
  subnet_id      = aws_subnet.deploy.id
  route_table_id = aws_route_table.deploy.id
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# --- Proxy security group: the one server allowed to reach the internet ---

resource "aws_security_group" "proxy" {
  name        = "${var.name}-proxy-sg"
  description = "Proxy instance: SSH from admin IP, forward-proxy + relay traffic from the AI instance only, open outbound"
  vpc_id      = data.aws_vpc.default.id

  tags = {
    Name = "${var.name}-proxy-sg"
  }
}

resource "aws_vpc_security_group_ingress_rule" "proxy_ssh" {
  security_group_id = aws_security_group.proxy.id
  description       = "SSH from admin IP only"
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
  cidr_ipv4         = var.admin_cidr
}

resource "aws_vpc_security_group_ingress_rule" "proxy_squid_from_ai" {
  security_group_id            = aws_security_group.proxy.id
  description                  = "Forward-proxy traffic from the AI instance"
  ip_protocol                  = "tcp"
  from_port                    = var.squid_port
  to_port                      = var.squid_port
  referenced_security_group_id = aws_security_group.ai.id
}

resource "aws_vpc_security_group_ingress_rule" "proxy_relay_from_ai" {
  security_group_id            = aws_security_group.proxy.id
  description                  = "Internal relay API traffic from the AI instance"
  ip_protocol                  = "tcp"
  from_port                    = var.relay_port
  to_port                      = var.relay_port
  referenced_security_group_id = aws_security_group.ai.id
}

resource "aws_vpc_security_group_ingress_rule" "proxy_squid_from_deploy" {
  security_group_id            = aws_security_group.proxy.id
  description                  = "Forward-proxy traffic from the deploy instance"
  ip_protocol                  = "tcp"
  from_port                    = var.squid_port
  to_port                      = var.squid_port
  referenced_security_group_id = aws_security_group.deploy.id
}

resource "aws_vpc_security_group_ingress_rule" "proxy_relay_from_deploy" {
  security_group_id            = aws_security_group.proxy.id
  description                  = "Internal relay API traffic from the deploy instance (status queries)"
  ip_protocol                  = "tcp"
  from_port                    = var.relay_port
  to_port                      = var.relay_port
  referenced_security_group_id = aws_security_group.deploy.id
}

resource "aws_vpc_security_group_egress_rule" "proxy_all" {
  security_group_id = aws_security_group.proxy.id
  description       = "Allow all outbound traffic (internet access)"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# --- AI security group: no path to the internet, reachable only from the proxy ---

resource "aws_security_group" "ai" {
  name        = "${var.name}-ai-sg"
  description = "AI instance: SSH only via the proxy (jump host), relay traffic to/from the proxy only, no other traffic in or out"
  vpc_id      = data.aws_vpc.default.id

  tags = {
    Name = "${var.name}-ai-sg"
  }
}

resource "aws_vpc_security_group_ingress_rule" "ai_ssh_from_proxy" {
  security_group_id            = aws_security_group.ai.id
  description                  = "SSH from the proxy instance only (jump host - admin IP has no route here)"
  ip_protocol                  = "tcp"
  from_port                    = 22
  to_port                      = 22
  referenced_security_group_id = aws_security_group.proxy.id
}

resource "aws_vpc_security_group_ingress_rule" "ai_relay_from_proxy" {
  security_group_id            = aws_security_group.ai.id
  description                  = "Internal relay API traffic from the proxy instance"
  ip_protocol                  = "tcp"
  from_port                    = var.relay_port
  to_port                      = var.relay_port
  referenced_security_group_id = aws_security_group.proxy.id
}

resource "aws_vpc_security_group_egress_rule" "ai_squid_to_proxy" {
  security_group_id            = aws_security_group.ai.id
  description                  = "Forward-proxy traffic to the proxy instance only"
  ip_protocol                  = "tcp"
  from_port                    = var.squid_port
  to_port                      = var.squid_port
  referenced_security_group_id = aws_security_group.proxy.id
}

resource "aws_vpc_security_group_egress_rule" "ai_relay_to_proxy" {
  security_group_id            = aws_security_group.ai.id
  description                  = "Internal relay API traffic to the proxy instance only"
  ip_protocol                  = "tcp"
  from_port                    = var.relay_port
  to_port                      = var.relay_port
  referenced_security_group_id = aws_security_group.proxy.id
}

# Deliberately no other egress rule on aws_security_group.ai - combined with the AI
# subnet's route table having no Internet Gateway route, this instance has no path to
# the internet at either the network layer or the security-group layer.

# --- Deploy security group: same isolation model as AI, plus HTTP forwarded from the proxy ---

resource "aws_security_group" "deploy" {
  name        = "${var.name}-deploy-sg"
  description = "Deploy instance: SSH only via the proxy (jump host), relay + forward-proxy traffic to/from the proxy only, HTTP forwarded from the proxy only - never reachable directly from the internet"
  vpc_id      = data.aws_vpc.default.id

  tags = {
    Name = "${var.name}-deploy-sg"
  }
}

resource "aws_vpc_security_group_ingress_rule" "deploy_ssh_from_proxy" {
  security_group_id            = aws_security_group.deploy.id
  description                  = "SSH from the proxy instance only (jump host - admin IP has no route here)"
  ip_protocol                  = "tcp"
  from_port                    = 22
  to_port                      = 22
  referenced_security_group_id = aws_security_group.proxy.id
}

resource "aws_vpc_security_group_ingress_rule" "deploy_relay_from_proxy" {
  security_group_id            = aws_security_group.deploy.id
  description                  = "Internal relay API traffic from the proxy instance (deploy triggers, status)"
  ip_protocol                  = "tcp"
  from_port                    = var.relay_port
  to_port                      = var.relay_port
  referenced_security_group_id = aws_security_group.proxy.id
}

resource "aws_vpc_security_group_ingress_rule" "deploy_http_from_proxy" {
  security_group_id            = aws_security_group.deploy.id
  description                  = "HTTP traffic to the currently-deployed container, reverse-proxied from the proxy instance only - the actual internet-facing gate lives on the proxy, approval-controlled"
  ip_protocol                  = "tcp"
  from_port                    = var.deploy_http_port
  to_port                      = var.deploy_http_port
  referenced_security_group_id = aws_security_group.proxy.id
}

resource "aws_vpc_security_group_egress_rule" "deploy_squid_to_proxy" {
  security_group_id            = aws_security_group.deploy.id
  description                  = "Forward-proxy traffic to the proxy instance only (git clone, container image pulls)"
  ip_protocol                  = "tcp"
  from_port                    = var.squid_port
  to_port                      = var.squid_port
  referenced_security_group_id = aws_security_group.proxy.id
}

resource "aws_vpc_security_group_egress_rule" "deploy_relay_to_proxy" {
  security_group_id            = aws_security_group.deploy.id
  description                  = "Internal relay API traffic to the proxy instance only"
  ip_protocol                  = "tcp"
  from_port                    = var.relay_port
  to_port                      = var.relay_port
  referenced_security_group_id = aws_security_group.proxy.id
}

# Deliberately no other egress rule on aws_security_group.deploy - same reasoning as
# the AI instance: combined with the deploy subnet's route table having no Internet
# Gateway route, this instance has no path to the internet at either layer, and no
# direct inbound path from the internet either. The proxy is the sole edge in both
# directions.

# --- Public HTTP ingress on the proxy, for reverse-proxying to the deploy instance ---
#
# The network path is always open (same posture as Squid's port, which is always
# reachable too) - the actual gate is at the application layer: a small reverse proxy
# on the proxy instance forwards to the deploy instance only while "open", and refuses
# otherwise. Neither instance holds AWS credentials, so toggling this SG rule itself at
# runtime isn't an option (and would be the wrong layer for this anyway - it's exactly
# the same allow/deny-by-content model already used for outbound domains).
resource "aws_vpc_security_group_ingress_rule" "proxy_http_public" {
  security_group_id = aws_security_group.proxy.id
  description       = "Public HTTP - reverse-proxied to the deploy instance only while approval-gated open state is active, refused (503) otherwise"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = "0.0.0.0/0"
}
