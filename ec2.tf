resource "aws_key_pair" "claude_signal" {
  key_name   = "${var.name}-key"
  public_key = var.ssh_public_key
}

resource "aws_instance" "proxy" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.proxy_instance_type
  subnet_id              = aws_subnet.proxy.id
  vpc_security_group_ids = [aws_security_group.proxy.id]
  key_name               = aws_key_pair.claude_signal.key_name

  instance_initiated_shutdown_behavior = "stop"

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.proxy_root_volume_size
    encrypted   = true
  }

  tags = {
    Name = "${var.name}-proxy-instance"
  }

  # data.aws_ami.ubuntu tracks "most_recent" - without this, a newer Ubuntu 22.04
  # AMI being published upstream would show up as a diff on every future plan/apply
  # and force a full destroy+recreate of a live, already-registered instance (lost
  # signal-cli identity, lost everything on the root volume) for no reason anyone
  # asked for. Rebuilding onto a newer AMI is still possible - just do it
  # deliberately (taint the resource, or drop this block) rather than by accident.
  lifecycle {
    ignore_changes = [ami]
  }
}

# An Elastic IP association survives stop/start (unlike the auto-assigned public IP
# this instance would otherwise get, which is released on every stop) - this is what
# lets var.app_domain point at a fixed address instead of needing DNS updated on every
# boot. Only created when a domain is actually configured (see acm.tf); without one,
# behavior is unchanged from before - a fresh public IP each start, found via /status.
resource "aws_eip" "proxy" {
  count    = var.app_domain != "" ? 1 : 0
  domain   = "vpc"
  instance = aws_instance.proxy.id

  tags = {
    Name = "${var.name}-proxy-eip"
  }
}

resource "aws_instance" "ai" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.ai_instance_type
  subnet_id              = aws_subnet.ai.id
  vpc_security_group_ids = [aws_security_group.ai.id]
  key_name               = aws_key_pair.claude_signal.key_name

  instance_initiated_shutdown_behavior = "stop"

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.ai_root_volume_size
    encrypted   = true
  }

  tags = {
    Name = "${var.name}-ai-instance"
  }

  # See the matching lifecycle block on aws_instance.proxy above for why.
  lifecycle {
    ignore_changes = [ami]
  }
}

resource "aws_instance" "deploy" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.deploy_instance_type
  subnet_id              = aws_subnet.deploy.id
  vpc_security_group_ids = [aws_security_group.deploy.id]
  key_name               = aws_key_pair.claude_signal.key_name

  instance_initiated_shutdown_behavior = "stop"

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.deploy_root_volume_size
    encrypted   = true
  }

  tags = {
    Name = "${var.name}-deploy-instance"
  }

  # See the matching lifecycle block on aws_instance.proxy above for why.
  lifecycle {
    ignore_changes = [ami]
  }
}

# Neither instance has an IAM instance profile - all three hold zero AWS credentials.
# Start/stop is triggered externally via the Lambda controller (see lambda.tf), which
# scopes ec2:StartInstances/StopInstances to instances tagged Project = var.name.
