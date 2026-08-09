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
}

# Neither instance has an IAM instance profile - all three hold zero AWS credentials.
# Start/stop is triggered externally via the Lambda controller (see lambda.tf), which
# scopes ec2:StartInstances/StopInstances to instances tagged Project = var.name.
