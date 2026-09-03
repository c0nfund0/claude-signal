variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "eu-north-1"
}

variable "name" {
  description = "Project name prefix used for resource naming and tagging"
  type        = string
  default     = "claude-signal"
}

variable "admin_cidr" {
  description = "CIDR block (your public IP, e.g. 203.0.113.5/32) allowed to SSH into the proxy instance"
  type        = string

  validation {
    condition     = can(cidrhost(var.admin_cidr, 0))
    error_message = "admin_cidr must be a valid CIDR block, e.g. 203.0.113.5/32."
  }
}

variable "ssh_public_key" {
  description = "Public key content (e.g. contents of ~/.ssh/id_ed25519.pub) used to SSH into both instances"
  type        = string
}

variable "stop_secret" {
  description = "Shared secret an on-instance idle monitor sends to authorize the Lambda's /stop route (e.g. output of `openssl rand -hex 32`)"
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.stop_secret) >= 32
    error_message = "stop_secret should be a long random string (32+ chars), e.g. `openssl rand -hex 32`."
  }
}

variable "ai_instance_type" {
  description = "EC2 instance type for the isolated AI (Claude Code) instance"
  type        = string
  default     = "t3.small"

  validation {
    condition     = contains(["t3.micro", "t3.small", "t3.medium"], var.ai_instance_type)
    error_message = "ai_instance_type must be one of: t3.micro, t3.small, t3.medium."
  }
}

variable "proxy_instance_type" {
  description = "EC2 instance type for the internet-facing proxy instance"
  type        = string
  default     = "t3.small"

  validation {
    condition     = contains(["t3.micro", "t3.small", "t3.medium"], var.proxy_instance_type)
    error_message = "proxy_instance_type must be one of: t3.micro, t3.small, t3.medium."
  }
}

variable "ai_root_volume_size" {
  description = "Root EBS volume size in GB for the AI instance"
  type        = number
  default     = 20
}

variable "proxy_root_volume_size" {
  description = "Root EBS volume size in GB for the proxy instance"
  type        = number
  default     = 20
}

variable "ai_subnet_cidr" {
  description = "CIDR block for the private AI subnet (no route to any Internet Gateway)"
  type        = string
  # 172.31.128.0/24 conflicts with an existing cs2-server-subnet in this account's
  # default VPC - .130.0/24 avoids that plus the default VPC's own .0/20, .16.0/20,
  # .32.0/20 subnets and this project's own .129.0/24 proxy subnet.
  default = "172.31.130.0/24"
}

variable "proxy_subnet_cidr" {
  description = "CIDR block for the public proxy subnet"
  type        = string
  default     = "172.31.129.0/24"
}

variable "availability_zone" {
  description = "Availability zone for both subnets and instances"
  type        = string
  default     = "eu-north-1a"
}

variable "squid_port" {
  description = "Port the proxy instance's forward proxy (e.g. Squid) will listen on for the AI instance's outbound traffic. Not yet installed by this Terraform config - reserved for the follow-up application-layer phase."
  type        = number
  default     = 3128
}

variable "relay_port" {
  description = "Port reserved for the internal relay API between the AI and proxy instances (Signal message delivery, URL-approval tool calls). Not yet used by anything - reserved for the follow-up application-layer phase."
  type        = number
  default     = 8443
}

variable "deploy_instance_type" {
  description = "EC2 instance type for the deploy/web instance"
  type        = string
  default     = "t3.micro"

  validation {
    condition     = contains(["t3.micro", "t3.small", "t3.medium"], var.deploy_instance_type)
    error_message = "deploy_instance_type must be one of: t3.micro, t3.small, t3.medium."
  }
}

variable "deploy_root_volume_size" {
  description = "Root EBS volume size in GB for the deploy/web instance"
  type        = number
  default     = 20
}

variable "deploy_subnet_cidr" {
  description = "CIDR block for the private deploy subnet (no route to any Internet Gateway - same isolation model as the AI subnet; the only path in or out is via the proxy instance)"
  type        = string
  default     = "172.31.131.0/24"
}

variable "deploy_http_port" {
  description = "Port the deploy instance's currently-running deployed container listens on, on the host. The proxy reverse-proxies public port 80 to this port on the deploy instance's private IP, gated by Signal approval - it is never exposed directly."
  type        = number
  default     = 8080
}

# --- Optional: single-link start (see README's "Custom domain" section) ---
# Leave both domains "" to skip this whole feature - no EIP, no ACM cert, no custom
# domain gets created, and behavior is exactly what it was before (a fresh public IP
# each start, `open`/`close` only via Signal).

variable "web_domain" {
  description = "Hostname (e.g. web.example.com) that starts the proxy+deploy instances and auto-opens the site when hit - mapped to the Lambda controller via an API Gateway custom domain. You manage DNS yourself (see README) - point this at the CNAME target `terraform output` gives you after the ACM cert is issued."
  type        = string
  default     = ""
}

variable "app_domain" {
  description = "Hostname (e.g. app.example.com) for the actual deployed site once it's up - served over HTTPS by the proxy's own nginx with a Let's Encrypt cert, at the stable Elastic IP this creates. Point an A record at `terraform output proxy_eip` once applied."
  type        = string
  default     = ""
}

variable "web_open_secret" {
  description = "Shared secret the Lambda controller sends (as X-Web-Open-Secret) to authorize opening the web gate without going through Signal, when var.web_domain is set - must match the same value in ansible/group_vars/all.yml, exactly like var.stop_secret. Required (32+ chars) only if web_domain is non-empty."
  type        = string
  default     = ""
  sensitive   = true

  validation {
    condition     = var.web_open_secret == "" || length(var.web_open_secret) >= 32
    error_message = "web_open_secret should be a long random string (32+ chars), e.g. `openssl rand -hex 32`."
  }
}
