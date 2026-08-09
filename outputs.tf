output "controller_url" {
  description = "Open this URL to start both servers and watch their status"
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "ai_instance_id" {
  description = "EC2 instance ID of the isolated AI (Claude Code) instance"
  value       = aws_instance.ai.id
}

output "proxy_instance_id" {
  description = "EC2 instance ID of the internet-facing proxy instance"
  value       = aws_instance.proxy.id
}

output "proxy_public_ip" {
  description = "Public IP of the proxy instance as of the last apply/refresh - changes on every stop/start, so prefer the controller URL's /status endpoint for the current value"
  value       = aws_instance.proxy.public_ip
}

output "ai_private_ip" {
  description = "Private IP of the AI instance (stable across stop/start) - the ssh -J target"
  value       = aws_instance.ai.private_ip
}

output "ai_subnet_id" {
  description = "Private subnet ID for the AI instance (no Internet Gateway route)"
  value       = aws_subnet.ai.id
}

output "proxy_subnet_id" {
  description = "Public subnet ID for the proxy instance"
  value       = aws_subnet.proxy.id
}

output "ai_security_group_id" {
  description = "Security group ID for the AI instance"
  value       = aws_security_group.ai.id
}

output "proxy_security_group_id" {
  description = "Security group ID for the proxy instance"
  value       = aws_security_group.proxy.id
}

output "deploy_instance_id" {
  description = "EC2 instance ID of the deploy/web instance"
  value       = aws_instance.deploy.id
}

output "deploy_private_ip" {
  description = "Private IP of the deploy instance (stable across stop/start) - the ssh -J target"
  value       = aws_instance.deploy.private_ip
}

output "deploy_subnet_id" {
  description = "Private subnet ID for the deploy instance (no Internet Gateway route)"
  value       = aws_subnet.deploy.id
}

output "deploy_security_group_id" {
  description = "Security group ID for the deploy instance"
  value       = aws_security_group.deploy.id
}
